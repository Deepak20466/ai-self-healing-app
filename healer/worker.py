"""healer-pod worker: the Procfile entrypoint (`python -m healer.worker`).

Dequeues `heal_jobs` with `SKIP LOCKED`, woken by `LISTEN/NOTIFY` (SPEC.md:
"no polling spin") with a bounded fallback wait so a missed/raced NOTIFY
can't stall the worker forever. Handles all three `HealJobType`s: `runtime_error`/
`contract_violation` go to `healer.runtime_agent.run_heal_job`, `ci_failure`
goes to `healer.ci_agent.run_ci_heal_job` (Phase 5) — one worker process,
one queue, dispatched by job type.
"""

from __future__ import annotations

import asyncio

import structlog

from core.config import settings
from core.db import dispose_engine, session_scope
from core.logging import configure_logging
from core.models import HealJob, HealJobStatus, HealJobType
from core.queue import HealJobListener, dequeue_heal_job
from healer.anthropic_client import AnthropicClientLike, build_anthropic_client
from healer.ci_agent import run_ci_heal_job
from healer.circuit_breaker import global_hourly_circuit_open
from healer.mcp_client import MCPToolClient, connect_http
from healer.runtime_agent import run_heal_job
from mcp_server.github_client import GitHubClient

logger = structlog.get_logger(__name__)

_ALL_JOB_TYPES = (HealJobType.RUNTIME_ERROR, HealJobType.CONTRACT_VIOLATION, HealJobType.CI_FAILURE)
_NOTIFY_FALLBACK_SECONDS = 30.0


async def _process_next_job(mcp: MCPToolClient, anthropic_client: AnthropicClientLike) -> bool:
    """Claim and (usually) run the next eligible job. Returns whether a job was claimed."""
    async with session_scope() as session:
        job = await dequeue_heal_job(session, types=_ALL_JOB_TYPES)
        if job is None:
            return False
        job_id = job.id
        job_type = job.type

        if await global_hourly_circuit_open(
            session, max_per_hour=settings.max_heal_jobs_per_hour_global
        ):
            # Global throughput cap, not "this bug is unfixable" — put it
            # straight back on the queue instead of failing it.
            job.status = HealJobStatus.QUEUED
            job.started_at = None
            logger.warning("worker.global_hourly_cap_hit", heal_job_id=job_id)
            return True

    try:
        async with GitHubClient() as github:
            if job_type == HealJobType.CI_FAILURE:
                await run_ci_heal_job(
                    job_id, anthropic_client=anthropic_client, mcp=mcp, github=github
                )
            else:
                await run_heal_job(
                    job_id, anthropic_client=anthropic_client, mcp=mcp, github=github
                )
    except Exception:
        # A single job's unhandled failure (e.g. a GitHub API error while
        # opening the fallback issue) must never take down the whole
        # long-running worker process — mark this job failed and keep polling.
        logger.exception("worker.job_failed_unexpectedly", heal_job_id=job_id)
        async with session_scope() as session:
            job = await session.get(HealJob, job_id)
            if job is not None:
                job.status = HealJobStatus.FAILED
    return True


async def run_worker() -> None:
    configure_logging(settings.log_level)
    anthropic_client = build_anthropic_client()
    mcp_url = f"http://127.0.0.1:{settings.mcp_port}/mcp"

    async with connect_http(mcp_url) as mcp, HealJobListener() as listener:
        logger.info("worker.started", mcp_url=mcp_url)
        while True:
            claimed = await _process_next_job(mcp, anthropic_client)
            if claimed:
                continue
            try:
                async with asyncio.timeout(_NOTIFY_FALLBACK_SECONDS):
                    await listener.wait()
            except TimeoutError:
                continue


def main() -> None:
    try:
        asyncio.run(run_worker())
    finally:
        asyncio.run(dispose_engine())


if __name__ == "__main__":
    main()

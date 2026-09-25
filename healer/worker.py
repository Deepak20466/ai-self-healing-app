"""healer-pod worker: the Procfile entrypoint (`python -m healer.worker`, or
equivalently `python -m healer.main`).

Dequeues `heal_jobs` with `SKIP LOCKED`, woken by `LISTEN/NOTIFY` (SPEC.md:
"no polling spin") with a bounded fallback wait so a missed/raced NOTIFY
can't stall the worker forever. Handles all three `HealJobType`s, dispatched
by job type to one of two AI backends selected once at startup by
`settings.use_claude_code` (SPEC.md AI BACKENDS):

- Free mode (default): `healer.agent_free.run_heal_job_free`/
  `run_ci_heal_job_free`, driving the fix loop through the local Claude Code
  CLI on the user's subscription login — no `anthropic_client` needed.
- API mode: `healer.runtime_agent.run_heal_job`/`healer.ci_agent.
  run_ci_heal_job` via the `anthropic` SDK (an optional extra, imported
  lazily by `healer.anthropic_client.build_anthropic_client`).

Both backends share the same `(job_id, *, mcp, github, remote)`-shaped
interface (API mode's just also takes `anthropic_client`), so this dispatch
is the only place that needs to know which backend is active.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import structlog

from core.config import settings
from core.db import dispose_engine, session_scope
from core.logging import configure_logging
from core.models import HealJob, HealJobStatus, HealJobType, MonitoredApp
from core.queue import HealJobListener, dequeue_heal_job
from healer.circuit_breaker import global_hourly_circuit_open
from healer.mcp_client import MCPToolClient, connect_http
from mcp_server.github_client import GitHubClient

logger = structlog.get_logger(__name__)

_ALL_JOB_TYPES = (HealJobType.RUNTIME_ERROR, HealJobType.CONTRACT_VIOLATION, HealJobType.CI_FAILURE)
_NOTIFY_FALLBACK_SECONDS = 30.0


_JobRunner = Callable[..., Awaitable[None]]


@dataclass(frozen=True)
class _JobRunners:
    """The two entry points worker dispatch needs, whichever backend is active.

    Both are called the same way regardless of backend: `runner(job_id,
    mcp=mcp, github=github)` — API mode's extra `anthropic_client` argument is
    bound in ahead of time via `functools.partial`.
    """

    runtime_or_contract: _JobRunner
    ci_failure: _JobRunner


def _select_backend() -> _JobRunners:
    if settings.use_claude_code:
        from healer.agent_free import run_ci_heal_job_free, run_heal_job_free

        logger.info("worker.backend_selected", backend="free (Claude Code CLI)")
        return _JobRunners(runtime_or_contract=run_heal_job_free, ci_failure=run_ci_heal_job_free)

    from functools import partial

    from healer.anthropic_client import build_anthropic_client
    from healer.ci_agent import run_ci_heal_job
    from healer.runtime_agent import run_heal_job

    anthropic_client = build_anthropic_client()
    logger.info(
        "worker.backend_selected", backend="api (anthropic SDK)", model=settings.anthropic_model
    )
    return _JobRunners(
        runtime_or_contract=partial(run_heal_job, anthropic_client=anthropic_client),
        ci_failure=partial(run_ci_heal_job, anthropic_client=anthropic_client),
    )


async def _process_next_job(mcp: MCPToolClient, backend: _JobRunners) -> bool:
    """Claim and (usually) run the next eligible job. Returns whether a job was claimed."""
    async with session_scope() as session:
        job = await dequeue_heal_job(session, types=_ALL_JOB_TYPES)
        if job is None:
            return False
        job_id = job.id
        job_type = job.type
        github_repo: str | None = None
        if job.app_id is not None:
            app = await session.get(MonitoredApp, job.app_id)
            if app is not None:
                github_repo = app.github_repo

        if await global_hourly_circuit_open(
            session, max_per_hour=settings.max_heal_jobs_per_hour_global
        ):
            # Global throughput cap, not "this bug is unfixable" — put it
            # straight back on the queue instead of failing it. Return False
            # (not True): the caller's main loop treats True as "immediately
            # try again", which busy-spins forever on this same oldest queued
            # job the instant the cap is open — 100% CPU, log spam, and every
            # OTHER queued job starved indefinitely, since dequeue_heal_job's
            # FIFO order never lets a newer job get a turn. False makes the
            # loop back off and wait on the next notify/fallback timeout
            # instead, same as "no job available" — reproduced for real
            # running the live CI self-healing demo (job 338 vs. job 340).
            job.status = HealJobStatus.QUEUED
            job.started_at = None
            logger.warning("worker.global_hourly_cap_hit", heal_job_id=job_id)
            return False

    try:
        async with GitHubClient(repo=github_repo) as github:
            if job_type == HealJobType.CI_FAILURE:
                await backend.ci_failure(job_id, mcp=mcp, github=github)
            else:
                await backend.runtime_or_contract(job_id, mcp=mcp, github=github)
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
    backend = _select_backend()
    mcp_url = f"http://127.0.0.1:{settings.mcp_port}/mcp"

    async with connect_http(mcp_url) as mcp, HealJobListener() as listener:
        logger.info("worker.started", mcp_url=mcp_url)
        while True:
            claimed = await _process_next_job(mcp, backend)
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

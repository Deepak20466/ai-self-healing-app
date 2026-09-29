"""healer-pod worker: the Procfile entrypoint (`python -m healer.worker`, or
equivalently `python -m healer.main`).

Dequeues `heal_jobs` with `SKIP LOCKED`, woken by `LISTEN/NOTIFY` (SPEC.md:
"no polling spin") with a bounded fallback wait so a missed/raced NOTIFY
can't stall the worker forever. Handles all three `HealJobType`s, dispatched
by job type to one of six pluggable AI backends, chosen PER JOB from
`settings.ai_chain_list` (terminal-only v1.0 Step 4 -- `AI_CHAIN`, an
ordered comma list; unset, this is just `[settings.ai_backend]`, the
original single-backend behavior, unchanged):

- `"claude_cli"` (default): `healer.agent_free.run_heal_job_free`/
  `run_ci_heal_job_free`, driving the fix loop through the local Claude Code
  CLI on the user's subscription login — no `anthropic_client` needed.
- `"codex_cli"`: `healer.agent_codex.run_heal_job_codex`/
  `run_ci_heal_job_codex`, the local OpenAI Codex CLI. See that module's
  docstring for its "untested against a real install" status.
- `"gemini_cli"`: `healer.agent_gemini.run_heal_job_gemini`/
  `run_ci_heal_job_gemini`, the local Google Gemini CLI. Same caveat.
- `"api"`: `healer.runtime_agent.run_heal_job`/`healer.ci_agent.
  run_ci_heal_job` via the `anthropic` SDK (an optional extra, imported
  lazily by `healer.anthropic_client.build_anthropic_client`).
- `"gemini_api"`/`"groq_api"`: the SAME `run_heal_job`/`run_ci_heal_job`
  functions as `"api"`, just with a `healer.api_adapters.GeminiApiClient`/
  `GroqClient` in place of the real Anthropic client — see that module for
  why this reuse is possible (both satisfy the same narrow
  `AnthropicClientLike` Protocol).

All backends share the same `(job_id, *, mcp, github, remote)`-shaped
interface (the API-key ones also take `anthropic_client`, bound in ahead of
time via `functools.partial`), so `_select_backend` is the only place that
needs to know which backend is active — every import is lazy (inside the
matching branch), so a machine running one backend never needs the others'
CLIs/keys installed or configured at all.

`_process_next_job` resolves a fresh job's backend as the first chain entry
with a key and no active cooldown; a quota/rate-limit/auth error (`healer.
backend_chain.BackendCooldownError`, raised only by the two API-key
adapters) puts that backend in cooldown and requeues the SAME job so the
NEXT chain entry picks it up on a later dequeue (tracked via an
`audit_log` "backend_attempt" row per attempt, so a resurrected job never
retries a backend it already tried). If every chain entry is exhausted or
cooling down, the job is marked `paused_budget` instead of `failed` — the
same status `is_budget_paused` already uses, extended here to also mean
"the AI chain, not the dollar budget, is what's exhausted" (see
`selfheal status`/`GET /api/backends` for the distinction).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import structlog
from sqlalchemy import select

from core.config import settings
from core.db import dispose_engine, session_scope
from core.logging import configure_logging
from core.models import AuditLog, HealJob, HealJobStatus, HealJobType, MonitoredApp
from core.queue import HealJobListener, dequeue_heal_job
from core.scanner import detect_heavy_dependencies
from healer import backend_chain
from healer.circuit_breaker import global_hourly_circuit_open
from healer.findings_actions import SUGGEST_MODE_ACTION
from healer.mcp_client import MCPToolClient, connect_http
from healer.remote_verify import run_heal_job_remote_verify
from healer.suggest_mode import run_heal_job_suggest
from mcp_server.github_client import GitHubClient, GitHubClientError
from mcp_server.sandbox import REPO_ROOT

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


def _select_backend(name: str | None = None) -> _JobRunners:
    """Build the runner pair for one backend NAME. Defaults to
    `settings.ai_backend` (the pre-chain single-backend behavior, unchanged)
    when called with no argument — `healer/backend_chain.py`'s per-job
    chain dispatch calls this with an explicit name instead."""
    backend = name or settings.ai_backend

    if backend == "claude_cli":
        from healer.agent_free import run_ci_heal_job_free, run_heal_job_free

        logger.info("worker.backend_selected", backend="claude_cli (Claude Code CLI)")
        return _JobRunners(runtime_or_contract=run_heal_job_free, ci_failure=run_ci_heal_job_free)

    if backend == "codex_cli":
        from healer.agent_codex import run_ci_heal_job_codex, run_heal_job_codex

        logger.info("worker.backend_selected", backend="codex_cli (OpenAI Codex CLI)")
        return _JobRunners(runtime_or_contract=run_heal_job_codex, ci_failure=run_ci_heal_job_codex)

    if backend == "gemini_cli":
        from healer.agent_gemini import run_ci_heal_job_gemini, run_heal_job_gemini

        logger.info("worker.backend_selected", backend="gemini_cli (Google Gemini CLI)")
        return _JobRunners(
            runtime_or_contract=run_heal_job_gemini, ci_failure=run_ci_heal_job_gemini
        )

    if backend == "api":
        from functools import partial

        from healer.anthropic_client import build_anthropic_client
        from healer.ci_agent import run_ci_heal_job
        from healer.runtime_agent import run_heal_job

        anthropic_client = build_anthropic_client()
        logger.info(
            "worker.backend_selected",
            backend="api (anthropic SDK)",
            model=settings.anthropic_model,
        )
        return _JobRunners(
            runtime_or_contract=partial(run_heal_job, anthropic_client=anthropic_client),
            ci_failure=partial(run_ci_heal_job, anthropic_client=anthropic_client),
        )

    if backend == "gemini_api":
        from functools import partial

        from healer.anthropic_client import AnthropicClientLike
        from healer.api_adapters import GeminiApiClient
        from healer.ci_agent import run_ci_heal_job
        from healer.runtime_agent import run_heal_job

        client: AnthropicClientLike = GeminiApiClient()
        logger.info("worker.backend_selected", backend="gemini_api (Gemini free-tier HTTP API)")
        return _JobRunners(
            runtime_or_contract=partial(run_heal_job, anthropic_client=client),
            ci_failure=partial(run_ci_heal_job, anthropic_client=client),
        )

    if backend == "groq_api":
        from functools import partial

        from healer.anthropic_client import AnthropicClientLike
        from healer.api_adapters import GroqClient
        from healer.ci_agent import run_ci_heal_job
        from healer.runtime_agent import run_heal_job

        groq_client: AnthropicClientLike = GroqClient()
        logger.info("worker.backend_selected", backend="groq_api (Groq free-tier HTTP API)")
        return _JobRunners(
            runtime_or_contract=partial(run_heal_job, anthropic_client=groq_client),
            ci_failure=partial(run_ci_heal_job, anthropic_client=groq_client),
        )

    if backend == "openrouter_api":
        from functools import partial

        from healer.anthropic_client import AnthropicClientLike
        from healer.api_adapters import OpenRouterClient
        from healer.ci_agent import run_ci_heal_job
        from healer.runtime_agent import run_heal_job

        openrouter_client: AnthropicClientLike = OpenRouterClient()
        logger.info(
            "worker.backend_selected",
            backend="openrouter_api (OpenRouter free-tier HTTP API)",
            model=settings.anthropic_model,
        )
        return _JobRunners(
            runtime_or_contract=partial(run_heal_job, anthropic_client=openrouter_client),
            ci_failure=partial(run_ci_heal_job, anthropic_client=openrouter_client),
        )

    raise ValueError(
        f"unknown AI_BACKEND {backend!r}; expected one of "
        "'claude_cli', 'codex_cli', 'gemini_cli', 'api', 'gemini_api', 'groq_api', 'openrouter_api'"
    )


async def _tried_backends_for_job(session: Any, job_id: int) -> set[str]:
    stmt = select(AuditLog.details).where(
        AuditLog.heal_job_id == job_id, AuditLog.action == "backend_attempt"
    )
    rows = (await session.execute(stmt)).scalars().all()
    return {r["backend"] for r in rows if isinstance(r, dict) and r.get("backend")}


async def _attribute_backend_if_pr_opened(
    job_id: int, backend_name: str, github: GitHubClient
) -> None:
    """After a successful dispatch, if the job now has a PR, record which
    backend produced it (`healer/automerge.py` refuses to auto-merge a
    fallback-produced fix) and post one attribution comment. Best-effort —
    a GitHub error here must never turn a successful fix into a failure."""
    async with session_scope() as session:
        job = await session.get(HealJob, job_id)
        if job is None or job.pr_number is None or job.produced_by_backend is not None:
            return
        job.produced_by_backend = backend_name
        pr_number = job.pr_number
    try:
        await github.create_issue_comment(pr_number, f"_Produced by AI backend: `{backend_name}`._")
    except GitHubClientError:
        logger.warning(
            "worker.attribution_comment_failed", heal_job_id=job_id, backend=backend_name
        )


async def _process_next_job(mcp: MCPToolClient, backend: _JobRunners | None = None) -> bool:
    """Claim and (usually) run the next eligible job. Returns whether a job was claimed.

    `backend=None` (the normal `run_worker()` path) resolves the backend
    PER JOB from `settings.ai_chain_list` — a single-item chain (the
    default, from `ai_backend`) behaves exactly as before. Tests that pass
    an explicit `backend` keep the old single-backend behavior unchanged.
    """
    chosen_backend_name: str | None = None
    async with session_scope() as session:
        job = await dequeue_heal_job(session, types=_ALL_JOB_TYPES)
        if job is None:
            return False
        job_id = job.id
        job_type = job.type
        github_repo: str | None = None
        is_remote_verify_job = False
        is_suggest_job = False
        if job_type != HealJobType.CI_FAILURE:
            suggest_stmt = select(AuditLog.id).where(
                AuditLog.action == SUGGEST_MODE_ACTION, AuditLog.heal_job_id == job_id
            )
            is_suggest_job = (await session.execute(suggest_stmt)).scalar_one_or_none() is not None
        if job.app_id is not None:
            app = await session.get(MonitoredApp, job.app_id)
            if app is not None:
                github_repo = app.github_repo
                # A connected app whose manifest names a heavy/ML dependency
                # (torch/tensorflow/chromadb/...) never gets its deps
                # installed locally (standing project rule) -- route
                # runtime/contract-violation fixes through the CI-verified
                # path instead of the normal AI_CHAIN dispatch below. A
                # ci_failure job needs no such routing: it's already a
                # response to the connected repo's OWN CI, so there's
                # nothing to install here either way.
                if (
                    not is_suggest_job
                    and job_type != HealJobType.CI_FAILURE
                    and app.repo_url is not None
                    and detect_heavy_dependencies(REPO_ROOT / app.local_repo_path)
                ):
                    is_remote_verify_job = True

        bypasses_normal_dispatch = is_remote_verify_job or is_suggest_job
        if bypasses_normal_dispatch:
            # No DB write here -- exit the session_scope() block first and
            # dispatch after it closes, same principle as the nested-
            # session_scope() deadlock documented in CLAUDE.md's Phase 5 log
            # (an awaited external call must never run inside an already-open
            # transaction that a callee will open its OWN session_scope()
            # against).
            pass
        elif await global_hourly_circuit_open(
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

        if not bypasses_normal_dispatch and backend is None:
            chain = settings.ai_chain_list
            tried = await _tried_backends_for_job(session, job_id)
            chosen_backend_name = (
                backend_chain.next_eligible_after(chain, tried)
                if tried
                else backend_chain.first_eligible(chain)
            )
            if chosen_backend_name is None:
                job.status = HealJobStatus.PAUSED_BUDGET
                job.error_message = "all AI_CHAIN backends are exhausted or cooling down"
                session.add(
                    AuditLog(
                        action="all_backends_exhausted",
                        actor="healer",
                        heal_job_id=job_id,
                        details={"chain": chain},
                    )
                )
                logger.warning("worker.all_backends_exhausted", heal_job_id=job_id, chain=chain)
                return True
            session.add(
                AuditLog(
                    action="backend_attempt",
                    actor="healer",
                    heal_job_id=job_id,
                    details={"backend": chosen_backend_name},
                )
            )

    if is_suggest_job:
        async with GitHubClient(repo=github_repo) as github:
            await run_heal_job_suggest(job_id, mcp=mcp, github=github)
        return True

    if is_remote_verify_job:
        async with GitHubClient(repo=github_repo) as github:
            await run_heal_job_remote_verify(job_id, mcp=mcp, github=github)
        return True

    runners = backend if backend is not None else _select_backend(chosen_backend_name)
    try:
        async with GitHubClient(repo=github_repo) as github:
            if job_type == HealJobType.CI_FAILURE:
                await runners.ci_failure(job_id, mcp=mcp, github=github)
            else:
                await runners.runtime_or_contract(job_id, mcp=mcp, github=github)
            if chosen_backend_name is not None:
                await _attribute_backend_if_pr_opened(job_id, chosen_backend_name, github)
    except backend_chain.BackendCooldownError as exc:
        # A quota/rate-limit/auth error from an API-key backend: not "this
        # bug is unfixable", so put the job back on the queue (like the
        # global-cap path above) for the NEXT chain backend to pick up,
        # rather than marking it failed.
        if chosen_backend_name is not None:
            backend_chain.mark_cooldown(
                chosen_backend_name, retry_after_seconds=exc.retry_after_seconds
            )
        logger.warning(
            "worker.backend_cooldown",
            heal_job_id=job_id,
            backend=chosen_backend_name,
            error=str(exc),
        )
        async with session_scope() as session:
            job = await session.get(HealJob, job_id)
            if job is not None:
                job.status = HealJobStatus.QUEUED
                job.started_at = None
        return False
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
    logger.info("worker.ai_chain", chain=settings.ai_chain_list)
    mcp_url = settings.mcp_client_url or f"http://127.0.0.1:{settings.mcp_port}/mcp"

    async with connect_http(mcp_url) as mcp, HealJobListener() as listener:
        logger.info("worker.started", mcp_url=mcp_url)
        while True:
            # backend=None -> resolved per job from settings.ai_chain_list
            # (a single-item chain, the default, behaves exactly as before).
            claimed = await _process_next_job(mcp)
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

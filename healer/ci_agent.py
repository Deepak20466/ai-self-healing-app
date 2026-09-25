"""The `ci_failure` agentic fix loop (SPEC.md healer-pod, Phase 5).

`run_ci_heal_job` handles one CI-fix attempt for one PR: read the failing
job's logs via MCP, let the model classify the failure (flaky vs. a real
test/lint/type/dependency failure), and either trigger a single re-run (for
flaky) or fix forward on the PR's own branch, run the tests locally, and
push a fix commit — posting a PR comment with the outcome either way, per
SPEC.md's CI-failure bullet.

Unlike `healer.runtime_agent.run_heal_job` (which loops up to 3 attempts
*within one call*, because it can prove success itself via local pytest
runs), a CI-fix attempt's real verdict comes from GitHub Actions re-running
the workflow on the pushed commit — something this process doesn't wait
around for. So `run_ci_heal_job` runs exactly *one* attempt per call, and
`sentinel.storage.record_pipeline_event` requeues the *same* heal_job
(bumping `source_pipeline_run_id`) when that commit's CI fails again,
rather than this module looping internally. `healer.circuit_breaker.
ci_fix_circuit_open` (backed by `HealJob.attempt_count`, incremented once
per real attempt here) is what turns "requeue forever" into SPEC.md's "max 2
CI-fix attempts per PR".

Every guardrail SPEC.md lists is enforced in code, not only by
`healer/ci_prompts.py`'s system prompt: `propose_patch`'s write-scope and
anti-cheating checks (`mcp_server/patch_guard.py`) apply exactly as they do
for runtime fixes, and `rerun_workflow`'s `run_id`/`failed_only` arguments
are always overridden server-side to *this* job's actual pipeline run — a
compromised or confused model can't rerun (or claim it reran) a different
workflow run than the one it was asked to fix.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import structlog
from sqlalchemy import func, select

from core.config import settings
from core.db import session_scope
from core.models import (
    AuditLog,
    BudgetCategory,
    FixAttempt,
    HealJob,
    HealJobStatus,
    HealJobType,
    PipelineRun,
)
from healer.anthropic_client import AnthropicClientLike
from healer.budget import is_budget_paused, record_spend
from healer.ci_prompts import CI_SYSTEM_PROMPT, build_ci_initial_messages
from healer.circuit_breaker import ci_fix_circuit_open
from healer.costs import TokenUsage, compute_cost_usd
from healer.github_ops import CIFixOutcome, open_ci_needs_human_issue, post_ci_fix_comment
from healer.mcp_client import MCPToolClient, MCPToolError
from healer.worktree import commit_and_push, create_worktree_for_branch, remove_worktree
from mcp_server.github_client import GitHubClient

logger = structlog.get_logger(__name__)

MAX_TOOL_CALLS_PER_ATTEMPT = 20
CLAUDE_MAX_TOKENS = 8000

CI_TOOL_NAMES = frozenset(
    {
        "get_workflow_run",
        "get_job_logs",
        "rerun_workflow",
        "read_file",
        "search_code",
        "list_files",
        "get_git_blame",
        "get_recent_commits",
        "run_tests",
        "propose_patch",
    }
)


@dataclass(frozen=True)
class CIAttemptResult:
    outcome: str
    """One of "fixed", "flaky_rerun", "failed"."""
    root_cause: str
    diff_stat: str
    test_output: str
    input_tokens: int
    output_tokens: int
    cached_tokens: int
    cost_usd: Decimal


async def _audit(
    session: Any, *, action: str, heal_job_id: int | None, details: dict[str, Any] | None = None
) -> None:
    session.add(AuditLog(action=action, actor="healer", details=details, heal_job_id=heal_job_id))
    await session.flush()


async def _prior_tokens_for_job(session: Any, heal_job_id: int) -> int:
    stmt = select(
        func.coalesce(func.sum(FixAttempt.input_tokens + FixAttempt.output_tokens), 0)
    ).where(FixAttempt.heal_job_id == heal_job_id)
    result: int = (await session.execute(stmt)).scalar_one()
    return result


async def _open_needs_human_issue(
    github: GitHubClient, *, job_id: int, pr_number: int, reason: str
) -> None:
    """The GitHub half of "give up on this job": open a needs-human-review
    issue. Always call this *after* any `session_scope()` block that wrote
    this job's terminal status has already committed and closed — never
    from inside one.

    Hit this for real: an earlier version did the DB write and this GitHub
    call together in one helper, itself called from *inside* the caller's
    own still-open `session_scope()` block. That nested a second,
    independent transaction on the same `heal_jobs` row underneath one that
    was already open and uncommitted — a genuine deadlock (not just a slow
    query): the outer transaction was blocked in Python waiting for that
    coroutine to return, while its own inner transaction was blocked in
    Postgres waiting for the outer transaction's still-open row lock to
    release. Neither could ever proceed. `_prepare_job` below now only ever
    returns data — every DB write happens inside its own single
    `session_scope()` block, and this function (DB-free) always runs after
    that block has already committed and closed, which makes that shape of
    bug structurally impossible.
    """
    try:
        await open_ci_needs_human_issue(github, pr_number=pr_number, attempts_summary=reason)
    except Exception:  # noqa: BLE001 - opening the fallback issue must never crash the worker
        logger.exception("ci_agent.needs_human_issue_failed", heal_job_id=job_id)


@dataclass(frozen=True)
class _JobContext:
    """Everything the attempt loop needs, gathered by `_prepare_job`."""

    pr_number: int
    run_id: int
    workflow: str
    branch: str
    failed_job_name: str | None
    attempt_number: int
    max_attempts: int


@dataclass(frozen=True)
class _GiveUp:
    """`_prepare_job` says: stop, and open a needs-human-review issue for `pr_number`."""

    pr_number: int
    reason: str


async def _prepare_job(job_id: int) -> _JobContext | _GiveUp | None:
    """Validate and advance one `ci_failure` heal_job's state, in one
    `session_scope()` block, and report what the caller should do next.

    Returns `None` when there's nothing more to do and no issue is needed
    (job missing/wrong type, no `pr_number`, no linked `pipeline_run`, or
    budget-paused), a `_GiveUp` when the circuit breaker or token cap has
    been hit (a fallback issue should be opened), or a `_JobContext` when an
    attempt should proceed. Never calls GitHub itself — see
    `_open_needs_human_issue`'s docstring for why that split matters.
    """
    async with session_scope() as session:
        job = await session.get(HealJob, job_id)
        if job is None:
            logger.warning("ci_agent.job_not_found", heal_job_id=job_id)
            return None
        if job.type != HealJobType.CI_FAILURE:
            raise ValueError(f"run_ci_heal_job only handles ci_failure, got {job.type}")

        pr_number = job.pr_number
        attempt_number = job.attempt_count + 1
        max_attempts = settings.max_ci_fix_attempts_per_pr

        if pr_number is None:
            job.status = HealJobStatus.FAILED
            job.error_message = "ci_failure heal_job has no pr_number to comment on or fix"
            job.finished_at = datetime.now(UTC)
            await _audit(
                session,
                action="heal_failed",
                heal_job_id=job_id,
                details={"reason": job.error_message},
            )
            return None

        if await ci_fix_circuit_open(session, pr_number, max_attempts=max_attempts):
            job.status = HealJobStatus.FAILED
            job.error_message = "circuit breaker: too many CI-fix attempts for this PR"
            job.finished_at = datetime.now(UTC)
            await _audit(
                session,
                action="circuit_breaker_tripped",
                heal_job_id=job_id,
                details={"pr_number": pr_number},
            )
            return _GiveUp(
                pr_number=pr_number,
                reason=(
                    f"Exceeded the maximum of {max_attempts} automated CI-fix attempts for this PR."
                ),
            )

        pipeline_run = (
            await session.get(PipelineRun, job.source_pipeline_run_id)
            if job.source_pipeline_run_id is not None
            else None
        )
        if pipeline_run is None:
            job.status = HealJobStatus.FAILED
            job.error_message = "ci_failure heal_job has no linked pipeline_run"
            job.finished_at = datetime.now(UTC)
            await _audit(
                session,
                action="heal_failed",
                heal_job_id=job_id,
                details={"reason": job.error_message},
            )
            return None

        if await is_budget_paused(
            session, category=BudgetCategory.HEALER, daily_budget_usd=settings.daily_budget_usd
        ):
            job.status = HealJobStatus.PAUSED_BUDGET
            await _audit(
                session, action="budget_paused", heal_job_id=job_id, details={"category": "healer"}
            )
            return None

        prior_tokens = await _prior_tokens_for_job(session, job_id)
        if prior_tokens > settings.max_tokens_per_job:
            job.status = HealJobStatus.FAILED
            job.error_message = "exceeded MAX_TOKENS_PER_JOB for this heal_job"
            job.finished_at = datetime.now(UTC)
            await _audit(
                session,
                action="heal_failed",
                heal_job_id=job_id,
                details={"reason": job.error_message},
            )
            return _GiveUp(
                pr_number=pr_number,
                reason="Ran out of the per-job token budget before a fix could be verified.",
            )

        job.status = HealJobStatus.CI_FIXING
        job.attempt_count = attempt_number
        job.started_at = job.started_at or datetime.now(UTC)
        await _audit(
            session,
            action="ci_fix_attempt_started",
            heal_job_id=job_id,
            details={
                "pr_number": pr_number,
                "run_id": pipeline_run.run_id,
                "attempt_number": attempt_number,
            },
        )

        return _JobContext(
            pr_number=pr_number,
            run_id=pipeline_run.run_id,
            workflow=pipeline_run.workflow,
            branch=pipeline_run.branch,
            failed_job_name=pipeline_run.failed_job,
            attempt_number=attempt_number,
            max_attempts=max_attempts,
        )


async def run_ci_heal_job(
    job_id: int,
    *,
    anthropic_client: AnthropicClientLike,
    mcp: MCPToolClient,
    github: GitHubClient,
    remote: str = "origin",
) -> None:
    """Run one CI-fix attempt for one `ci_failure` heal_job.

    `remote` defaults to `origin` (production); tests override it to a
    throwaway local bare repo (see `tests/conftest.py`'s `fake_git_remote`).
    """
    prepared = await _prepare_job(job_id)
    if prepared is None:
        return
    if isinstance(prepared, _GiveUp):
        await _open_needs_human_issue(
            github, job_id=job_id, pr_number=prepared.pr_number, reason=prepared.reason
        )
        return

    pr_number = prepared.pr_number
    run_id = prepared.run_id
    workflow = prepared.workflow
    branch = prepared.branch
    failed_job_name = prepared.failed_job_name
    attempt_number = prepared.attempt_number
    max_attempts = prepared.max_attempts

    tool_schemas = [
        schema for schema in await mcp.list_tool_schemas() if schema["name"] in CI_TOOL_NAMES
    ]

    worktree_name = f"ci-heal-{job_id}-{attempt_number}"
    worktree_path = await create_worktree_for_branch(worktree_name, branch, remote=remote)

    try:
        messages = build_ci_initial_messages(
            run_id=run_id,
            workflow=workflow,
            branch=branch,
            pr_number=pr_number,
            failed_job_name=failed_job_name,
            attempt_number=attempt_number,
            max_attempts=max_attempts,
            heal_job_id=job_id,
            worktree=worktree_name,
            previous_attempts_summary=None,
        )
        result = await _run_ci_attempt(
            anthropic_client=anthropic_client,
            mcp=mcp,
            job_id=job_id,
            worktree_name=worktree_name,
            messages=messages,
            tool_schemas=tool_schemas,
            real_run_id=run_id,
        )

        async with session_scope() as session:
            session.add(
                FixAttempt(
                    heal_job_id=job_id,
                    attempt_number=attempt_number,
                    root_cause=result.root_cause,
                    diff=None,
                    test_output=result.test_output,
                    passed=result.outcome == "fixed",
                    input_tokens=result.input_tokens,
                    output_tokens=result.output_tokens,
                    cached_tokens=result.cached_tokens,
                    cost_usd=result.cost_usd,
                )
            )
            await record_spend(session, category=BudgetCategory.HEALER, cost_usd=result.cost_usd)

        if result.outcome == "fixed":
            commit_msg = (
                f"CI auto-fix: {workflow} run {run_id}\n\n"
                f"heal_job #{job_id}, attempt {attempt_number}"
            )
            await commit_and_push(worktree_path, branch, message=commit_msg, remote=remote)

        try:
            await post_ci_fix_comment(
                github,
                CIFixOutcome(
                    heal_job_id=job_id,
                    pr_number=pr_number,
                    attempt_number=attempt_number,
                    max_attempts=max_attempts,
                    run_id=run_id,
                    outcome=result.outcome,
                    root_cause=result.root_cause,
                    diff_stat=result.diff_stat,
                    test_output=result.test_output,
                ),
            )
        except Exception:  # noqa: BLE001 - a comment failure must never crash the worker
            logger.exception("ci_agent.pr_comment_failed", heal_job_id=job_id)

        async with session_scope() as session:
            job = await session.get(HealJob, job_id)
            assert job is not None
            await _audit(
                session,
                action="ci_fix_attempt_finished",
                heal_job_id=job_id,
                details={"outcome": result.outcome, "run_id": run_id},
            )

            if result.outcome == "failed":
                # Nothing was pushed, so no further real CI run will arrive
                # to requeue this job via record_pipeline_event — leaving it
                # non-terminal would dangle forever regardless of how many
                # attempts remain, so this always ends the job and opens a
                # fallback issue rather than only doing so once
                # `max_attempts` is reached.
                job.status = HealJobStatus.FAILED
                job.error_message = "attempt did not produce a passing local test run"
                job.finished_at = datetime.now(UTC)
                await _audit(
                    session,
                    action="heal_failed",
                    heal_job_id=job_id,
                    details={"reason": job.error_message},
                )
            else:
                # "fixed" (pushed, awaiting real CI) or "flaky_rerun" (rerun
                # triggered, awaiting its result): both leave the job
                # in-flight so the next CI event on this PR requeues it.
                job.status = HealJobStatus.CI_FIXING

        # Outside the session above (committed and closed) — an awaited
        # GitHub call must never happen while a heal_jobs row's transaction
        # is still open: it would hold that row's lock for as long as the
        # call takes, and every other write to the same job (e.g. this same
        # method's own next attempt) would block behind it. Every other
        # branch in this function already keeps DB writes and GitHub calls
        # in separate, sequential blocks — this one didn't, and a slow or
        # stalled GitHub call here could wedge the whole worker.
        if result.outcome == "failed":
            try:
                await open_ci_needs_human_issue(
                    github, pr_number=pr_number, attempts_summary=result.root_cause
                )
            except Exception:  # noqa: BLE001 - must never crash the worker
                logger.exception("ci_agent.needs_human_issue_failed", heal_job_id=job_id)
    finally:
        await remove_worktree(worktree_name, branch)


async def _run_ci_attempt(
    *,
    anthropic_client: AnthropicClientLike,
    mcp: MCPToolClient,
    job_id: int,
    worktree_name: str,
    messages: list[dict[str, Any]],
    tool_schemas: list[dict[str, Any]],
    real_run_id: int,
) -> CIAttemptResult:
    """One conversation with Claude: read logs, classify, and either rerun
    or fix+test, until it stops (or triggers a rerun, which ends the
    attempt immediately)."""
    total_input = total_output = total_cached = 0
    total_cost = Decimal("0")
    test_results: list[bool] = []
    last_diff_stat = ""
    last_test_output = ""
    final_text = ""
    rerun_triggered = False

    for _ in range(MAX_TOOL_CALLS_PER_ATTEMPT):
        response = await anthropic_client.messages.create(
            model=settings.anthropic_model,
            max_tokens=CLAUDE_MAX_TOKENS,
            thinking={"type": "disabled"},
            system=[
                {"type": "text", "text": CI_SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}
            ],
            tools=tool_schemas,
            messages=messages,
        )

        usage = response.usage
        input_tokens = usage.input_tokens
        output_tokens = usage.output_tokens
        cache_write = getattr(usage, "cache_creation_input_tokens", 0) or 0
        cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
        total_input += input_tokens
        total_output += output_tokens
        total_cached += cache_read
        total_cost += compute_cost_usd(
            settings.anthropic_model,
            TokenUsage(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cache_creation_input_tokens=cache_write,
                cache_read_input_tokens=cache_read,
            ),
        )

        tool_use_blocks = [block for block in response.content if block.type == "tool_use"]
        text_blocks = [block.text for block in response.content if block.type == "text"]
        if text_blocks:
            final_text = " ".join(text_blocks)

        if not tool_use_blocks:
            break

        messages.append({"role": "assistant", "content": response.content})

        tool_results: list[dict[str, Any]] = []
        for block in tool_use_blocks:
            # Never trust the model's own heal_job_id/worktree/run_id — the
            # write scope propose_patch derives from heal_job_id, and the
            # run rerun_workflow reruns, must always be *this* job's, never
            # a caller-supplied value an injected payload could redirect.
            arguments = dict(block.input)
            if block.name in ("propose_patch", "run_tests"):
                arguments["worktree"] = worktree_name
            if block.name == "propose_patch":
                arguments["heal_job_id"] = job_id
            if block.name == "rerun_workflow":
                arguments["run_id"] = real_run_id
                arguments["failed_only"] = True

            is_error = False
            try:
                result = await mcp.call_tool(block.name, arguments)
            except MCPToolError as exc:
                result = str(exc)
                is_error = True

            if not is_error and block.name == "run_tests" and isinstance(result, dict):
                test_results.append(bool(result.get("passed")))
                last_test_output = str(result.get("output", ""))[-4000:]
            if not is_error and block.name == "propose_patch" and isinstance(result, dict):
                last_diff_stat = str(result.get("diff_stat", ""))
            if not is_error and block.name == "rerun_workflow":
                rerun_triggered = True

            content = result if isinstance(result, str) else json.dumps(result, default=str)
            tool_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": content,
                    "is_error": is_error,
                }
            )

        messages.append({"role": "user", "content": tool_results})

        if rerun_triggered:
            break

    if rerun_triggered:
        outcome = "flaky_rerun"
    elif test_results and test_results[-1]:
        outcome = "fixed"
    else:
        outcome = "failed"

    return CIAttemptResult(
        outcome=outcome,
        root_cause=final_text or "(agent produced no final summary)",
        diff_stat=last_diff_stat,
        test_output=last_test_output,
        input_tokens=total_input,
        output_tokens=total_output,
        cached_tokens=total_cached,
        cost_usd=total_cost,
    )

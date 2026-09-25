"""Circuit breakers: max heal attempts per fingerprint/24h, max heal jobs/hour
globally, and max CI-fix attempts per PR (SPEC.md SAFETY GUARDRAILS).

The fingerprint/global breakers count `heal_job` *rows* — the row currently
being considered was already inserted (queued) before the worker picks it
up, so "count > max" correctly means "this would be the (max+1)th job", not
"the (max)th".

The CI-fix breaker is different: SPEC.md's CI-fix loop pushes fix commits to
the *same* PR branch and reacts to further CI runs on that branch, so
`sentinel.storage.record_pipeline_event` reuses a single in-flight
`ci_failure` heal_job across repeated CI failures on one PR (requeuing it,
not inserting a new row each time) rather than one row per attempt — see its
docstring. That means attempts for one PR are counted by summing that job's
`attempt_count` column (which the CI agent increments once per real attempt
it runs), not by counting rows: `ci_fix_attempt_count_for_pr` therefore sums
`HealJob.attempt_count` across every `ci_failure` row for that `pr_number`
(normally just the one, reused, row) rather than counting rows.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.models import HealJob, HealJobType


async def fingerprint_job_count_24h(session: AsyncSession, fingerprint: str) -> int:
    since = datetime.now(UTC) - timedelta(hours=24)
    stmt = (
        select(func.count())
        .select_from(HealJob)
        .where(HealJob.fingerprint == fingerprint, HealJob.created_at >= since)
    )
    return (await session.execute(stmt)).scalar_one()


async def fingerprint_circuit_open(
    session: AsyncSession, fingerprint: str, *, max_attempts: int
) -> bool:
    """True if this fingerprint has already had more than `max_attempts` jobs in 24h."""
    return await fingerprint_job_count_24h(session, fingerprint) > max_attempts


async def global_job_count_last_hour(session: AsyncSession) -> int:
    since = datetime.now(UTC) - timedelta(hours=1)
    stmt = select(func.count()).select_from(HealJob).where(HealJob.created_at >= since)
    return (await session.execute(stmt)).scalar_one()


async def global_hourly_circuit_open(session: AsyncSession, *, max_per_hour: int) -> bool:
    """True if more than `max_per_hour` heal_jobs (any fingerprint) started in the last hour."""
    return await global_job_count_last_hour(session) > max_per_hour


async def ci_fix_attempt_count_for_pr(session: AsyncSession, pr_number: int) -> int:
    stmt = select(func.coalesce(func.sum(HealJob.attempt_count), 0)).where(
        HealJob.type == HealJobType.CI_FAILURE, HealJob.pr_number == pr_number
    )
    return (await session.execute(stmt)).scalar_one()


async def ci_fix_circuit_open(session: AsyncSession, pr_number: int, *, max_attempts: int) -> bool:
    """True if this PR has already used up its CI-fix attempts.

    Unlike the row-counting breakers above, `attempt_count` only increments
    once an attempt actually *runs* (see `healer.ci_agent.run_ci_heal_job`),
    so this compares with `>=`, not `>`: a job about to run its
    `max_attempts`-th attempt has an `attempt_count` of `max_attempts - 1`
    going in, which must still be allowed through.
    """
    return await ci_fix_attempt_count_for_pr(session, pr_number) >= max_attempts

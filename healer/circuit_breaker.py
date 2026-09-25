"""Circuit breakers: max heal attempts per fingerprint/24h, max heal jobs/hour
globally, and max CI-fix attempts per PR (SPEC.md SAFETY GUARDRAILS).

The fingerprint breaker sums `HealJob.attempt_count` (real attempts actually
run), not rows — same principle as the CI-fix breaker below. A row-counting
version was tried first and caused a real cascading lockout: a heal_job that
itself got circuit-broken (or otherwise never ran an attempt) still inserted
a row with `attempt_count == 0`, so three such never-attempted rows alone
exhausted the fingerprint's whole 24h budget and permanently blocked every
future *real* attempt for that error for the rest of the window, even though
zero actual fixes had been tried. Summing real attempts means only jobs that
actually ran count against the cap.

The global hourly breaker still counts rows deliberately: it exists to bound
worker throughput/API-call *volume* per SPEC.md ("max heal jobs/hour
globally"), not per-fingerprint fix budget, so a job that is itself refused
by another breaker before running still legitimately used a worker "slot"
this hour.

The CI-fix breaker: SPEC.md's CI-fix loop pushes fix commits to the *same*
PR branch and reacts to further CI runs on that branch, so
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


async def fingerprint_attempt_count_24h(session: AsyncSession, fingerprint: str) -> int:
    since = datetime.now(UTC) - timedelta(hours=24)
    stmt = select(func.coalesce(func.sum(HealJob.attempt_count), 0)).where(
        HealJob.fingerprint == fingerprint, HealJob.created_at >= since
    )
    return (await session.execute(stmt)).scalar_one()


async def fingerprint_circuit_open(
    session: AsyncSession, fingerprint: str, *, max_attempts: int
) -> bool:
    """True if this fingerprint has already used up its real attempts in 24h.

    Compares with `>=`, not `>`: a job about to run its `max_attempts`-th
    attempt has `attempt_count` summed to `max_attempts - 1` going in, which
    must still be allowed through (same reasoning as `ci_fix_circuit_open`).
    """
    return await fingerprint_attempt_count_24h(session, fingerprint) >= max_attempts


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

"""core.metrics: MTTR, success rate, cost-per-fix, and friends.

Unlike the MCP tools (which own their own session), these functions take a
session parameter directly, so the rollback-wrapped `db_session` fixture
gives full isolation *between metrics tests themselves*. It does NOT isolate
them from Phase 4+'s `healer` tests, which use `session_scope()` (real
commits, since `run_heal_job`/`run_ci_heal_job` need their own writes visible
across session boundaries) and so leave real `heal_jobs`/`fix_attempts` rows
behind in the shared `selfheal_test` DB — as of Phase 5, for *every*
`HealJobType`, `ci_failure` included (`tests/test_healer_ci_agent.py`'s
circuit-breaker test commits a real terminal `ci_failure` job). `compute_
fix_success_rate`/`compute_cost_per_fix_usd` are deliberately *global*,
unscoped queries (that's the right behavior for a real dashboard), so tests
that need an exact number read a baseline first and assert the delta, same
pattern as `tests/test_healer_circuit_breaker.py`'s global-hourly-cap test —
never by assuming any table or job type starts empty.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from core import metrics
from core.metrics import _TERMINAL_STATUSES
from core.models import Deployment, FixAttempt, HealJob, HealJobStatus, HealJobType


async def _terminal_and_verified_counts(db_session: AsyncSession) -> tuple[int, int]:
    total = (
        await db_session.execute(
            select(func.count()).select_from(HealJob).where(HealJob.status.in_(_TERMINAL_STATUSES))
        )
    ).scalar_one()
    verified = (
        await db_session.execute(
            select(func.count())
            .select_from(HealJob)
            .where(HealJob.status.in_(_TERMINAL_STATUSES), HealJob.status == HealJobStatus.VERIFIED)
        )
    ).scalar_one()
    return total, verified


async def _terminal_and_verified_counts_for_type(
    db_session: AsyncSession, job_type: HealJobType
) -> tuple[int, int]:
    total = (
        await db_session.execute(
            select(func.count())
            .select_from(HealJob)
            .where(HealJob.status.in_(_TERMINAL_STATUSES), HealJob.type == job_type)
        )
    ).scalar_one()
    verified = (
        await db_session.execute(
            select(func.count())
            .select_from(HealJob)
            .where(
                HealJob.status.in_(_TERMINAL_STATUSES),
                HealJob.type == job_type,
                HealJob.status == HealJobStatus.VERIFIED,
            )
        )
    ).scalar_one()
    return total, verified


async def _total_fix_attempt_cost(db_session: AsyncSession) -> Decimal:
    return (
        await db_session.execute(select(func.coalesce(func.sum(FixAttempt.cost_usd), Decimal("0"))))
    ).scalar_one()


async def _make_job(
    db_session: AsyncSession,
    *,
    status: HealJobStatus,
    job_type: HealJobType = HealJobType.RUNTIME_ERROR,
    started_minutes_ago: int = 20,
    finished_minutes_ago: int = 5,
) -> HealJob:
    now = datetime.now(UTC)
    finished_at = (
        None if status == HealJobStatus.QUEUED else now - timedelta(minutes=finished_minutes_ago)
    )
    job = HealJob(
        type=job_type,
        status=status,
        fingerprint=f"fp-{id(object())}",
        # created_at ("detection") must be set explicitly too - it has a
        # server_default, but MTTR is created_at -> finished_at, and the
        # server_default would otherwise always be "now" regardless of
        # started_minutes_ago.
        created_at=now - timedelta(minutes=started_minutes_ago),
        started_at=now - timedelta(minutes=started_minutes_ago),
        finished_at=finished_at,
    )
    db_session.add(job)
    await db_session.flush()
    return job


async def test_mttr_averages_only_verified_jobs(db_session: AsyncSession) -> None:
    await _make_job(
        db_session,
        status=HealJobStatus.VERIFIED,
        started_minutes_ago=20,
        finished_minutes_ago=10,
    )  # 10 minutes
    await _make_job(
        db_session, status=HealJobStatus.FAILED, started_minutes_ago=20, finished_minutes_ago=0
    )  # should be excluded

    mttr = await metrics.compute_mttr_minutes(db_session)
    assert mttr == 10.0


async def test_mttr_is_none_with_no_verified_jobs(db_session: AsyncSession) -> None:
    await _make_job(db_session, status=HealJobStatus.FAILED)
    mttr = await metrics.compute_mttr_minutes(db_session)
    assert mttr is None


async def test_fix_success_rate_counts_verified_over_terminal(db_session: AsyncSession) -> None:
    baseline_total, baseline_verified = await _terminal_and_verified_counts(db_session)

    await _make_job(db_session, status=HealJobStatus.VERIFIED)
    await _make_job(db_session, status=HealJobStatus.VERIFIED)
    await _make_job(db_session, status=HealJobStatus.FAILED)
    await _make_job(db_session, status=HealJobStatus.QUEUED)  # not terminal, excluded

    rate = await metrics.compute_fix_success_rate(db_session)
    assert rate == round((baseline_verified + 2) / (baseline_total + 3), 4)


async def test_fix_success_rate_ignores_non_terminal_jobs(db_session: AsyncSession) -> None:
    # Can no longer assume any job_type starts at zero terminal jobs (see
    # module docstring) - so this tests the QUEUED-jobs-never-count property
    # via a before/after delta instead of an absolute "is None", which
    # holds whether or not other tests have already left real ci_failure
    # terminal rows in the shared DB.
    before = await metrics.compute_fix_success_rate(db_session, job_types=(HealJobType.CI_FAILURE,))
    await _make_job(db_session, status=HealJobStatus.QUEUED, job_type=HealJobType.CI_FAILURE)
    after = await metrics.compute_fix_success_rate(db_session, job_types=(HealJobType.CI_FAILURE,))
    assert after == before


async def test_ci_auto_fix_rate_only_counts_ci_failure_jobs(db_session: AsyncSession) -> None:
    baseline_total, baseline_verified = await _terminal_and_verified_counts_for_type(
        db_session, HealJobType.CI_FAILURE
    )

    await _make_job(db_session, status=HealJobStatus.VERIFIED, job_type=HealJobType.CI_FAILURE)
    await _make_job(db_session, status=HealJobStatus.FAILED, job_type=HealJobType.CI_FAILURE)
    await _make_job(db_session, status=HealJobStatus.VERIFIED, job_type=HealJobType.RUNTIME_ERROR)

    rate = await metrics.compute_ci_auto_fix_rate(db_session)
    assert rate == round((baseline_verified + 1) / (baseline_total + 2), 4)


async def test_rollback_count(db_session: AsyncSession) -> None:
    db_session.add(Deployment(sha="a" * 40, env="production", status="rolled_back"))
    db_session.add(Deployment(sha="b" * 40, env="production", status="healthy"))
    await db_session.flush()

    count = await metrics.compute_rollback_count(db_session)
    assert count == 1


async def test_cost_per_fix_averages_only_successful_jobs(db_session: AsyncSession) -> None:
    baseline_total_cost = await _total_fix_attempt_cost(db_session)

    verified_job = await _make_job(db_session, status=HealJobStatus.VERIFIED)
    failed_job = await _make_job(db_session, status=HealJobStatus.FAILED)

    db_session.add(
        FixAttempt(heal_job_id=verified_job.id, attempt_number=1, cost_usd=Decimal("0.50"))
    )
    db_session.add(
        FixAttempt(heal_job_id=failed_job.id, attempt_number=1, cost_usd=Decimal("0.30"))
    )
    await db_session.flush()

    # cost_per_fix is scoped to VERIFIED jobs, which nothing outside this test
    # produces (see module docstring), so it needs no baseline.
    cost_per_fix, total_cost = await metrics.compute_cost_per_fix_usd(db_session)
    assert cost_per_fix == 0.50
    assert total_cost == round(float(baseline_total_cost) + 0.80, 4)


async def test_cost_per_fix_is_none_with_no_successful_jobs(db_session: AsyncSession) -> None:
    baseline_total_cost = await _total_fix_attempt_cost(db_session)

    cost_per_fix, total_cost = await metrics.compute_cost_per_fix_usd(db_session)
    assert cost_per_fix is None
    assert total_cost == round(float(baseline_total_cost), 4)


async def test_get_metrics_summary_is_internally_consistent(db_session: AsyncSession) -> None:
    baseline_total, baseline_verified = await _terminal_and_verified_counts(db_session)

    await _make_job(db_session, status=HealJobStatus.VERIFIED)
    summary = await metrics.get_metrics_summary(db_session, daily_budget_usd=2.0)

    assert summary.daily_budget_usd == 2.0
    assert summary.daily_budget_remaining_usd <= summary.daily_budget_usd
    assert summary.fix_success_rate == round((baseline_verified + 1) / (baseline_total + 1), 4)

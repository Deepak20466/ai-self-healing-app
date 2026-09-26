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
from core.metrics import _OUTCOME_STATUSES, NO_PROD_DEPLOY_LABEL, SUCCESS_STATUSES
from core.models import Deployment, FixAttempt, HealJob, HealJobStatus, HealJobType


async def _terminal_and_verified_counts(db_session: AsyncSession) -> tuple[int, int]:
    total = (
        await db_session.execute(
            select(func.count()).select_from(HealJob).where(HealJob.status.in_(_OUTCOME_STATUSES))
        )
    ).scalar_one()
    verified = (
        await db_session.execute(
            select(func.count()).select_from(HealJob).where(HealJob.status.in_(SUCCESS_STATUSES))
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
            .where(HealJob.status.in_(_OUTCOME_STATUSES), HealJob.type == job_type)
        )
    ).scalar_one()
    verified = (
        await db_session.execute(
            select(func.count())
            .select_from(HealJob)
            .where(
                HealJob.status.in_(_OUTCOME_STATUSES),
                HealJob.type == job_type,
                HealJob.status.in_(SUCCESS_STATUSES),
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
    pr_opened_minutes_ago: int | None = None,
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
        pr_opened_at=(
            None
            if pr_opened_minutes_ago is None
            else now - timedelta(minutes=pr_opened_minutes_ago)
        ),
    )
    db_session.add(job)
    await db_session.flush()
    return job


async def _pr_durations_minutes(db_session: AsyncSession) -> list[float]:
    since = datetime.now(UTC) - timedelta(days=30)
    rows = (
        await db_session.execute(
            select(HealJob.created_at, HealJob.pr_opened_at).where(
                HealJob.pr_opened_at.is_not(None), HealJob.created_at >= since
            )
        )
    ).all()
    return [(pr - created).total_seconds() / 60 for created, pr in rows]


async def test_mttr_is_detection_to_pr_opened(db_session: AsyncSession) -> None:
    before = await _pr_durations_minutes(db_session)
    await _make_job(
        db_session, status=HealJobStatus.PR_OPENED, started_minutes_ago=20, pr_opened_minutes_ago=10
    )  # 10 minutes to a PR
    await _make_job(
        db_session, status=HealJobStatus.FAILED, started_minutes_ago=20, finished_minutes_ago=0
    )  # no PR: excluded
    after = [*before, 10.0]

    mttr = await metrics.compute_mttr_minutes(db_session)
    assert mttr == round(sum(after) / len(after), 2)


async def test_mttr_ignores_jobs_without_a_pr(db_session: AsyncSession) -> None:
    before = await metrics.compute_mttr_minutes(db_session)
    await _make_job(db_session, status=HealJobStatus.FAILED)
    assert await metrics.compute_mttr_minutes(db_session) == before


async def test_pr_opened_counts_as_success_and_verified_is_separate(
    db_session: AsyncSession,
) -> None:
    baseline_total, baseline_ok = await _terminal_and_verified_counts(db_session)
    for status in (HealJobStatus.PR_OPENED, HealJobStatus.MERGED, HealJobStatus.DEPLOYED):
        await _make_job(db_session, status=status)
    await _make_job(db_session, status=HealJobStatus.FAILED)
    rate = await metrics.compute_fix_success_rate(db_session)
    assert rate == round((baseline_ok + 3) / (baseline_total + 4), 4)

    summary = await metrics.get_metrics_summary(db_session, daily_budget_usd=2.0)
    verified_exists = (
        await db_session.execute(
            select(func.count())
            .select_from(HealJob)
            .where(HealJob.status == HealJobStatus.VERIFIED)
        )
    ).scalar_one()
    if verified_exists == 0:
        assert summary.verified_in_production_rate is None
        assert summary.verified_in_production_label == NO_PROD_DEPLOY_LABEL


async def test_verified_in_production_rate_needs_a_verified_job(db_session: AsyncSession) -> None:
    await _make_job(db_session, status=HealJobStatus.VERIFIED)
    await _make_job(db_session, status=HealJobStatus.PR_OPENED)
    rate = await metrics.compute_verified_in_production_rate(db_session)
    assert rate is not None and 0 < rate < 1


async def test_fix_success_rate_counts_verified_over_terminal(db_session: AsyncSession) -> None:
    baseline_total, baseline_verified = await _terminal_and_verified_counts(db_session)

    await _make_job(db_session, status=HealJobStatus.PR_OPENED)
    await _make_job(db_session, status=HealJobStatus.MERGED)
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

    await _make_job(db_session, status=HealJobStatus.PR_OPENED, job_type=HealJobType.CI_FAILURE)
    await _make_job(db_session, status=HealJobStatus.FAILED, job_type=HealJobType.CI_FAILURE)
    await _make_job(db_session, status=HealJobStatus.PR_OPENED, job_type=HealJobType.RUNTIME_ERROR)

    rate = await metrics.compute_ci_auto_fix_rate(db_session)
    assert rate == round((baseline_verified + 1) / (baseline_total + 2), 4)


async def test_rollback_count(db_session: AsyncSession) -> None:
    db_session.add(Deployment(sha="a" * 40, env="production", status="rolled_back"))
    db_session.add(Deployment(sha="b" * 40, env="production", status="healthy"))
    await db_session.flush()

    count = await metrics.compute_rollback_count(db_session)
    assert count == 1


async def _successful_jobs_and_cost(db_session: AsyncSession) -> tuple[int, Decimal]:
    n = (
        await db_session.execute(
            select(func.count()).select_from(HealJob).where(HealJob.status.in_(SUCCESS_STATUSES))
        )
    ).scalar_one()
    cost = (
        await db_session.execute(
            select(func.coalesce(func.sum(FixAttempt.cost_usd), Decimal("0")))
            .join(HealJob, FixAttempt.heal_job_id == HealJob.id)
            .where(HealJob.status.in_(SUCCESS_STATUSES))
        )
    ).scalar_one()
    return n, cost


async def test_cost_per_fix_averages_only_successful_jobs(db_session: AsyncSession) -> None:
    baseline_total_cost = await _total_fix_attempt_cost(db_session)
    n, cost = await _successful_jobs_and_cost(db_session)

    ok_job = await _make_job(db_session, status=HealJobStatus.PR_OPENED)
    failed_job = await _make_job(db_session, status=HealJobStatus.FAILED)
    db_session.add(FixAttempt(heal_job_id=ok_job.id, attempt_number=1, cost_usd=Decimal("0.50")))
    db_session.add(
        FixAttempt(heal_job_id=failed_job.id, attempt_number=1, cost_usd=Decimal("0.30"))
    )
    await db_session.flush()

    cost_per_fix, total_cost = await metrics.compute_cost_per_fix_usd(db_session)
    assert cost_per_fix == round(float(cost + Decimal("0.50")) / (n + 1), 4)
    assert total_cost == round(float(baseline_total_cost) + 0.80, 4)


async def test_get_metrics_summary_is_internally_consistent(db_session: AsyncSession) -> None:
    baseline_total, baseline_verified = await _terminal_and_verified_counts(db_session)

    await _make_job(db_session, status=HealJobStatus.PR_OPENED)
    summary = await metrics.get_metrics_summary(db_session, daily_budget_usd=2.0)

    assert summary.daily_budget_usd == 2.0
    assert summary.daily_budget_remaining_usd <= summary.daily_budget_usd
    assert summary.fix_success_rate == round((baseline_verified + 1) / (baseline_total + 1), 4)


async def _attempted_counts(db_session: AsyncSession) -> tuple[int, int]:
    """(attempted outcome jobs, attempted successful jobs) currently in the DB."""
    has_attempt = select(FixAttempt.id).where(FixAttempt.heal_job_id == HealJob.id).exists()
    total = (
        await db_session.execute(
            select(func.count())
            .select_from(HealJob)
            .where(HealJob.status.in_(_OUTCOME_STATUSES), has_attempt)
        )
    ).scalar_one()
    ok = (
        await db_session.execute(
            select(func.count())
            .select_from(HealJob)
            .where(HealJob.status.in_(SUCCESS_STATUSES), has_attempt)
        )
    ).scalar_one()
    return total, ok


async def test_ai_fix_success_rate_excludes_jobs_blocked_before_an_attempt(
    db_session: AsyncSession,
) -> None:
    total, ok = await _attempted_counts(db_session)
    all_before = await metrics.compute_fix_success_rate(db_session)

    won = await _make_job(db_session, status=HealJobStatus.PR_OPENED)
    lost = await _make_job(db_session, status=HealJobStatus.FAILED)
    for job in (won, lost):
        db_session.add(FixAttempt(heal_job_id=job.id, attempt_number=1, cost_usd=Decimal("0")))
    # Blocked before any AI attempt (e.g. circuit breaker): no fix_attempts row.
    await _make_job(db_session, status=HealJobStatus.FAILED)
    await _make_job(db_session, status=HealJobStatus.FAILED)
    await db_session.flush()

    ai_rate = await metrics.compute_ai_fix_success_rate(db_session)
    assert ai_rate == round((ok + 1) / (total + 2), 4)
    # The all-time rate still counts the blocked jobs, so it is the lower one here.
    all_after = await metrics.compute_fix_success_rate(db_session)
    assert all_after is not None and ai_rate is not None
    assert all_after < ai_rate or all_before is None


async def test_summary_exposes_both_success_rates(db_session: AsyncSession) -> None:
    job = await _make_job(db_session, status=HealJobStatus.PR_OPENED)
    db_session.add(FixAttempt(heal_job_id=job.id, attempt_number=1, cost_usd=Decimal("0")))
    await db_session.flush()
    summary = await metrics.get_metrics_summary(db_session, daily_budget_usd=2.0)
    assert summary.ai_fix_success_rate is not None
    assert summary.fix_success_rate is not None

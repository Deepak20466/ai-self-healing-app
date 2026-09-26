"""Metrics used by SPEC.md's dashboard, the MCP `get_metrics` tool, chat's
"show stats", and `scripts/export_metrics.py` — one shared, tested source of
truth for every metric definition so the numbers agree everywhere they're
shown.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from pydantic import BaseModel
from sqlalchemy import ColumnElement, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.models import (
    Anomaly,
    ContractViolation,
    DailySpend,
    Deployment,
    Error,
    FixAttempt,
    HealJob,
    HealJobStatus,
    HealJobType,
)

# A job "succeeded" once its fix PR is open: the healer only opens a PR after
# proving the regression test fails before the fix and passes after, so
# `pr_opened` already implies passing tests. There is no production deploy
# step in this setup, so requiring `verified` (the old definition) made every
# metric read empty despite real fixes.
SUCCESS_STATUSES = (
    HealJobStatus.PR_OPENED,
    HealJobStatus.MERGED,
    HealJobStatus.DEPLOYED,
    HealJobStatus.VERIFIED,
)
# Jobs with a final outcome. In-flight (queued/running/ci_fixing/paused) jobs
# are excluded so they neither help nor hurt the rate.
_OUTCOME_STATUSES = (*SUCCESS_STATUSES, HealJobStatus.FAILED, HealJobStatus.ROLLED_BACK)
NO_PROD_DEPLOY_LABEL = "n/a (no production deploy)"


class ErrorsByTypePoint(BaseModel):
    day: date
    exception_type: str
    count: int


class MetricsSummary(BaseModel):
    mttr_minutes: float | None
    #: headline: success among jobs where the AI attempted a fix
    ai_fix_success_rate: float | None
    #: all-time: success among every job with a final outcome (incl. blocked)
    fix_success_rate: float | None
    #: stricter metric: verified healthy in production (needs a real deploy)
    verified_in_production_rate: float | None
    verified_in_production_label: str
    ci_auto_fix_rate: float | None
    contract_violation_catches: int
    rollback_count: int
    cost_per_fix_usd: float | None
    total_cost_usd: float
    daily_spend_usd: float
    daily_budget_usd: float
    daily_budget_remaining_usd: float
    open_anomalies: int
    errors_by_type: list[ErrorsByTypePoint]


async def compute_mttr_minutes(session: AsyncSession, *, window_days: int = 30) -> float | None:
    """Mean minutes from detection (`created_at`) to the fix PR being opened (`pr_opened_at`)."""
    since = datetime.now(UTC) - timedelta(days=window_days)
    stmt = select(func.avg(func.extract("epoch", HealJob.pr_opened_at - HealJob.created_at))).where(
        HealJob.pr_opened_at.is_not(None),
        HealJob.created_at >= since,
    )
    seconds = (await session.execute(stmt)).scalar_one_or_none()
    return None if seconds is None else round(float(seconds) / 60, 2)


async def _rate(
    session: AsyncSession,
    success: tuple[HealJobStatus, ...],
    job_types: tuple[HealJobType, ...] | None,
    *,
    attempted_only: bool = False,
) -> float | None:
    base_filter: list[ColumnElement[bool]] = [HealJob.status.in_(_OUTCOME_STATUSES)]
    if attempted_only:
        # Jobs blocked before any AI attempt (circuit breaker, duplicate,
        # budget, refusal) never wrote a fix_attempt row.
        base_filter.append(
            select(FixAttempt.id).where(FixAttempt.heal_job_id == HealJob.id).exists()
        )
    if job_types:
        base_filter.append(HealJob.type.in_(job_types))
    total = (
        await session.execute(select(func.count()).select_from(HealJob).where(*base_filter))
    ).scalar_one()
    if total == 0:
        return None
    ok = (
        await session.execute(
            select(func.count())
            .select_from(HealJob)
            .where(*base_filter, HealJob.status.in_(success))
        )
    ).scalar_one()
    return round(ok / total, 4)


async def compute_fix_success_rate(
    session: AsyncSession, *, job_types: tuple[HealJobType, ...] | None = None
) -> float | None:
    """Jobs whose PR was opened with passing tests / jobs with a final outcome."""
    return await _rate(session, SUCCESS_STATUSES, job_types)


async def compute_ai_fix_success_rate(session: AsyncSession) -> float | None:
    """Headline rate: PR opened / jobs where the AI actually attempted a fix."""
    return await _rate(session, SUCCESS_STATUSES, None, attempted_only=True)


async def compute_verified_in_production_rate(session: AsyncSession) -> float | None:
    """The strict version: only `verified` (healthy after a real deploy) counts.

    None unless at least one job was verified, since without a production
    deploy step the honest answer is "not applicable", not 0%.
    """
    verified = (
        await session.execute(
            select(func.count())
            .select_from(HealJob)
            .where(HealJob.status == HealJobStatus.VERIFIED)
        )
    ).scalar_one()
    if verified == 0:
        return None
    return await _rate(session, (HealJobStatus.VERIFIED,), None)


async def compute_ci_auto_fix_rate(session: AsyncSession) -> float | None:
    return await compute_fix_success_rate(session, job_types=(HealJobType.CI_FAILURE,))


async def compute_contract_violation_catches(session: AsyncSession) -> int:
    stmt = select(func.count()).select_from(ContractViolation)
    return (await session.execute(stmt)).scalar_one()


async def compute_rollback_count(session: AsyncSession) -> int:
    stmt = select(func.count()).select_from(Deployment).where(Deployment.status == "rolled_back")
    return (await session.execute(stmt)).scalar_one()


async def compute_cost_per_fix_usd(session: AsyncSession) -> tuple[float | None, float]:
    """(average cost per successful fix, total cost across all fix attempts)."""
    total_stmt = select(func.coalesce(func.sum(FixAttempt.cost_usd), Decimal("0")))
    total_cost = (await session.execute(total_stmt)).scalar_one()

    successful_jobs_stmt = (
        select(func.count()).select_from(HealJob).where(HealJob.status.in_(SUCCESS_STATUSES))
    )
    successful_jobs = (await session.execute(successful_jobs_stmt)).scalar_one()

    if successful_jobs == 0:
        return None, round(float(total_cost), 4)

    cost_of_successful_stmt = (
        select(func.coalesce(func.sum(FixAttempt.cost_usd), Decimal("0")))
        .select_from(FixAttempt)
        .join(HealJob, FixAttempt.heal_job_id == HealJob.id)
        .where(HealJob.status.in_(SUCCESS_STATUSES))
    )
    cost_of_successful = (await session.execute(cost_of_successful_stmt)).scalar_one()

    return round(float(cost_of_successful) / successful_jobs, 4), round(float(total_cost), 4)


async def compute_daily_spend_usd(session: AsyncSession, *, day: date | None = None) -> float:
    target_day = day or datetime.now(UTC).date()
    stmt = select(func.coalesce(func.sum(DailySpend.spend_usd), Decimal("0"))).where(
        DailySpend.day == target_day
    )
    total = (await session.execute(stmt)).scalar_one()
    return round(float(total), 4)


async def compute_open_anomaly_count(session: AsyncSession, *, window_hours: int = 24) -> int:
    since = datetime.now(UTC) - timedelta(hours=window_hours)
    stmt = select(func.count()).select_from(Anomaly).where(Anomaly.created_at >= since)
    return (await session.execute(stmt)).scalar_one()


async def compute_errors_by_type_over_time(
    session: AsyncSession, *, window_days: int = 14
) -> list[ErrorsByTypePoint]:
    since = datetime.now(UTC) - timedelta(days=window_days)
    day_expr = func.date_trunc("day", Error.created_at).label("day")
    # Label as "error_count", not "count" - Row already has a .count() method,
    # which would shadow a same-named attribute access below.
    stmt = (
        select(day_expr, Error.exception_type, func.count().label("error_count"))
        .where(Error.created_at >= since)
        .group_by(day_expr, Error.exception_type)
        .order_by(day_expr)
    )
    rows = (await session.execute(stmt)).all()
    return [
        ErrorsByTypePoint(
            day=row.day.date(), exception_type=row.exception_type, count=row.error_count
        )
        for row in rows
    ]


async def get_metrics_summary(session: AsyncSession, *, daily_budget_usd: float) -> MetricsSummary:
    mttr = await compute_mttr_minutes(session)
    success_rate = await compute_fix_success_rate(session)
    ai_rate = await compute_ai_fix_success_rate(session)
    ci_rate = await compute_ci_auto_fix_rate(session)
    verified_rate = await compute_verified_in_production_rate(session)
    violations = await compute_contract_violation_catches(session)
    rollbacks = await compute_rollback_count(session)
    cost_per_fix, total_cost = await compute_cost_per_fix_usd(session)
    daily_spend = await compute_daily_spend_usd(session)
    anomalies = await compute_open_anomaly_count(session)
    errors_by_type = await compute_errors_by_type_over_time(session)

    return MetricsSummary(
        mttr_minutes=mttr,
        ai_fix_success_rate=ai_rate,
        fix_success_rate=success_rate,
        ci_auto_fix_rate=ci_rate,
        verified_in_production_rate=verified_rate,
        verified_in_production_label=(
            NO_PROD_DEPLOY_LABEL if verified_rate is None else f"{verified_rate:.0%}"
        ),
        contract_violation_catches=violations,
        rollback_count=rollbacks,
        cost_per_fix_usd=cost_per_fix,
        total_cost_usd=total_cost,
        daily_spend_usd=daily_spend,
        daily_budget_usd=daily_budget_usd,
        daily_budget_remaining_usd=round(max(daily_budget_usd - daily_spend, 0.0), 4),
        open_anomalies=anomalies,
        errors_by_type=errors_by_type,
    )

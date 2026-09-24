"""Metrics used by SPEC.md's dashboard, the MCP `get_metrics` tool, chat's
"show stats", and `scripts/export_metrics.py` — one shared, tested source of
truth for every metric definition so the numbers agree everywhere they're
shown.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from pydantic import BaseModel
from sqlalchemy import func, select
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

_TERMINAL_STATUSES = (
    HealJobStatus.VERIFIED,
    HealJobStatus.FAILED,
    HealJobStatus.ROLLED_BACK,
)


class ErrorsByTypePoint(BaseModel):
    day: date
    exception_type: str
    count: int


class MetricsSummary(BaseModel):
    mttr_minutes: float | None
    fix_success_rate: float | None
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
    """Mean minutes from detection (`created_at`) to verified-healthy (`finished_at`)."""
    since = datetime.now(UTC) - timedelta(days=window_days)
    stmt = select(func.avg(func.extract("epoch", HealJob.finished_at - HealJob.created_at))).where(
        HealJob.status == HealJobStatus.VERIFIED,
        HealJob.finished_at.is_not(None),
        HealJob.created_at >= since,
    )
    seconds = (await session.execute(stmt)).scalar_one_or_none()
    return None if seconds is None else round(float(seconds) / 60, 2)


async def compute_fix_success_rate(
    session: AsyncSession, *, job_types: tuple[HealJobType, ...] | None = None
) -> float | None:
    """`verified` jobs / all jobs that reached a terminal state, among `job_types`."""
    base_filter = [HealJob.status.in_(_TERMINAL_STATUSES)]
    if job_types:
        base_filter.append(HealJob.type.in_(job_types))

    total_stmt = select(func.count()).select_from(HealJob).where(*base_filter)
    total = (await session.execute(total_stmt)).scalar_one()
    if total == 0:
        return None

    verified_stmt = (
        select(func.count())
        .select_from(HealJob)
        .where(*base_filter, HealJob.status == HealJobStatus.VERIFIED)
    )
    verified = (await session.execute(verified_stmt)).scalar_one()
    return round(verified / total, 4)


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
        select(func.count()).select_from(HealJob).where(HealJob.status == HealJobStatus.VERIFIED)
    )
    successful_jobs = (await session.execute(successful_jobs_stmt)).scalar_one()

    if successful_jobs == 0:
        return None, round(float(total_cost), 4)

    cost_of_successful_stmt = (
        select(func.coalesce(func.sum(FixAttempt.cost_usd), Decimal("0")))
        .select_from(FixAttempt)
        .join(HealJob, FixAttempt.heal_job_id == HealJob.id)
        .where(HealJob.status == HealJobStatus.VERIFIED)
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
    ci_rate = await compute_ci_auto_fix_rate(session)
    violations = await compute_contract_violation_catches(session)
    rollbacks = await compute_rollback_count(session)
    cost_per_fix, total_cost = await compute_cost_per_fix_usd(session)
    daily_spend = await compute_daily_spend_usd(session)
    anomalies = await compute_open_anomaly_count(session)
    errors_by_type = await compute_errors_by_type_over_time(session)

    return MetricsSummary(
        mttr_minutes=mttr,
        fix_success_rate=success_rate,
        ci_auto_fix_rate=ci_rate,
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

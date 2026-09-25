"""Daily spend tracking and hard-cap pausing (SPEC.md COST CONTROL).

`daily_spend` has one row per (day, category). `is_budget_paused` both reads
and — the first time the cap is crossed — latches `paused=True`, so a caller
that checks once per Claude API call gets a cheap, idempotent read on every
call after the first crossing instead of recomputing the sum each time.
Resets naturally the next UTC day, since `day` is part of the row's key.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from core.models import BudgetCategory, DailySpend


async def _get_or_create_row(
    session: AsyncSession, *, category: BudgetCategory, day: datetime | None = None
) -> DailySpend:
    target_day = (day or datetime.now(UTC)).date()
    stmt = (
        select(DailySpend)
        .where(DailySpend.day == target_day, DailySpend.category == category)
        .with_for_update()
    )
    row = (await session.execute(stmt)).scalar_one_or_none()
    if row is not None:
        return row

    insert_stmt = (
        pg_insert(DailySpend)
        .values(day=target_day, category=category, spend_usd=Decimal("0"), paused=False)
        .on_conflict_do_nothing(constraint="uq_daily_spend_day_category")
    )
    await session.execute(insert_stmt)
    return (await session.execute(stmt)).scalar_one()


async def is_budget_paused(
    session: AsyncSession, *, category: BudgetCategory, daily_budget_usd: Decimal
) -> bool:
    """True if today's spend for `category` has hit (or already latched past) the cap."""
    row = await _get_or_create_row(session, category=category)
    if row.paused:
        return True
    if row.spend_usd >= daily_budget_usd:
        row.paused = True
        await session.flush()
        return True
    return False


async def record_spend(
    session: AsyncSession, *, category: BudgetCategory, cost_usd: Decimal
) -> DailySpend:
    """Add `cost_usd` to today's running total for `category`. Caller commits."""
    row = await _get_or_create_row(session, category=category)
    row.spend_usd = row.spend_usd + cost_usd
    await session.flush()
    return row

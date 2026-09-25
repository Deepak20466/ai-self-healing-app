"""healer.budget: daily spend tracking and pause-at-cap (SPEC.md COST CONTROL).

Tests use the shared `isolated_budget_date` fixture from conftest.py, which
freezes the date to a synthetic far-future date so each test run uses a
different day and never collides with other test dates or real production
data in the shared `selfheal_test` database.
"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from core.models import BudgetCategory
from healer.budget import is_budget_paused, record_spend


async def test_not_paused_when_under_budget(isolated_budget_date, db_session: AsyncSession) -> None:
    paused = await is_budget_paused(
        db_session, category=BudgetCategory.HEALER, daily_budget_usd=Decimal("1.00")
    )
    assert paused is False


async def test_paused_once_spend_reaches_cap(
    isolated_budget_date, db_session: AsyncSession
) -> None:
    await record_spend(db_session, category=BudgetCategory.HEALER, cost_usd=Decimal("1.00"))
    paused = await is_budget_paused(
        db_session, category=BudgetCategory.HEALER, daily_budget_usd=Decimal("1.00")
    )
    assert paused is True


async def test_paused_flag_latches_on_repeated_checks(
    isolated_budget_date, db_session: AsyncSession
) -> None:
    await record_spend(db_session, category=BudgetCategory.HEALER, cost_usd=Decimal("2.00"))
    first = await is_budget_paused(
        db_session, category=BudgetCategory.HEALER, daily_budget_usd=Decimal("1.00")
    )
    second = await is_budget_paused(
        db_session, category=BudgetCategory.HEALER, daily_budget_usd=Decimal("1.00")
    )
    assert first is True
    assert second is True


async def test_categories_are_independent(isolated_budget_date, db_session: AsyncSession) -> None:
    await record_spend(db_session, category=BudgetCategory.CHAT, cost_usd=Decimal("5.00"))
    healer_paused = await is_budget_paused(
        db_session, category=BudgetCategory.HEALER, daily_budget_usd=Decimal("1.00")
    )
    assert healer_paused is False


async def test_record_spend_accumulates_on_the_same_row(
    isolated_budget_date, db_session: AsyncSession
) -> None:
    first = await record_spend(db_session, category=BudgetCategory.HEALER, cost_usd=Decimal("0.30"))
    second = await record_spend(
        db_session, category=BudgetCategory.HEALER, cost_usd=Decimal("0.30")
    )
    assert second.id == first.id
    assert second.spend_usd == Decimal("0.60")

"""Regression test for contract_violation #42 (heal_job #325): the
timezone silent bug on GET /orders/102/delivery-estimate.

Lives here rather than under tests/ because a contract_violation heal_job's
patch scope is restricted to apps/target_app/ (see mcp_server/sandbox.py).
Uses a stub session instead of a real DB connection so this test doesn't
depend on tests/conftest.py's database wiring, which pytest never loads for
a test file outside the tests/ package tree.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

from apps.target_app import bugs


@dataclass
class _FakeOrder:
    created_at: datetime


class _FakeSession:
    """Duck-typed stand-in for AsyncSession: only `.get()` is used by repository.get_order."""

    def __init__(self, order: _FakeOrder) -> None:
        self._order = order

    async def get(self, model: Any, order_id: int) -> _FakeOrder:
        return self._order


async def test_estimate_delivery_date_uses_storefront_local_calendar_day() -> None:
    # Postgres always normalizes timestamptz to UTC on read: an order placed
    # at 2026-01-02 01:15 IST round-trips as this UTC instant. A naive
    # .date() on it reads the wrong (previous) calendar day.
    order = _FakeOrder(created_at=datetime(2026, 1, 1, 19, 45, tzinfo=UTC))
    session = _FakeSession(order)

    estimated = await bugs.estimate_delivery_date(session, order_id=102)

    assert estimated == date(2026, 1, 5)

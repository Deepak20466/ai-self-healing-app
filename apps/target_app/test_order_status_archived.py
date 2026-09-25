"""Regression test for heal_job #332: KeyError('archived') in order_status_label.

Lives under apps/target_app/ (the runtime-fix write scope) rather than
tests/, so tests/conftest.py's db_session fixture and DATABASE_URL override
never reach it. Monkeypatches repository.get_order instead of hitting a real
DB, exercising exactly the bug (the ORDER_STATUS_LABELS dict lookup in
bugs.order_status_label) with no Postgres dependency.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from apps.target_app import bugs, seed_data


async def test_order_status_label_handles_archived_status(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_get_order(session: Any, order_id: int) -> Any:
        return SimpleNamespace(id=order_id, status="archived")

    monkeypatch.setattr(bugs.repository, "get_order", fake_get_order)

    label = await bugs.order_status_label(None, seed_data.ARCHIVED_ORDER_ID)

    assert label == "Archived"

"""Regression test for error #131 (heal_job #355): AttributeError raised by
item_label() when the item id doesn't exist (GET /trigger/none_lookup).

Lives here rather than under tests/ because a runtime_error heal_job's patch
scope is restricted to apps/target_app/ (see mcp_server/sandbox.py). Uses a
stub session instead of a real DB connection, matching
test_delivery_estimate_timezone.py's pattern.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import HTTPException

from apps.target_app import bugs


class _FakeSession:
    """Duck-typed stand-in for AsyncSession: `.get()` always misses, like a
    real lookup for an item id that was never seeded."""

    async def get(self, model: Any, item_id: int) -> None:
        return None


async def test_item_label_raises_404_for_a_missing_item() -> None:
    session = _FakeSession()

    with pytest.raises(HTTPException) as exc_info:
        await bugs.item_label(session, item_id=999)

    assert exc_info.value.status_code == 404

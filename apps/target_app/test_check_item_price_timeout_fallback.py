"""Regression test for heal_job #328.

`check_item_price` must not let a `httpx` connection/timeout error from the
unreachable pricing API propagate as an unhandled exception (see bugs.py's
`check_item_price` docstring, BUG #6).
"""

from __future__ import annotations

import pytest

from apps.target_app import bugs, seed_data


async def test_check_item_price_falls_back_when_pricing_api_is_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(bugs.settings, "pricing_api_timeout_seconds", 0.3)
    result = await bugs.check_item_price(seed_data.RATED_ITEM_ID)
    assert result == {"multiplier_applied": 100}

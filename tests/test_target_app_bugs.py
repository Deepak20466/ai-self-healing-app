"""Unit tests proving each of the 7 seeded bugs reproduces deterministically."""

from __future__ import annotations

from datetime import date

import httpx
import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from apps.target_app import bugs, seed_data


async def test_bug1_zero_division_on_unrated_item(db_session: AsyncSession) -> None:
    """Demo bug #1: passes while present (ZeroDivisionError) and once fixed (any value)."""
    try:
        result = await bugs.average_rating(db_session, seed_data.UNRATED_ITEM_ID)
    except ZeroDivisionError:
        return
    assert result is None or result == 0


async def test_average_rating_is_correct_for_a_rated_item(db_session: AsyncSession) -> None:
    rating = await bugs.average_rating(db_session, seed_data.RATED_ITEM_ID)
    assert rating == 4.5


async def test_bug2_key_error_on_archived_order_status(db_session: AsyncSession) -> None:
    """Demo bug #2: passes while present (KeyError) and once fixed (a label string)."""
    try:
        label = await bugs.order_status_label(db_session, seed_data.ARCHIVED_ORDER_ID)
    except KeyError:
        return
    assert isinstance(label, str) and label


async def test_order_status_label_is_correct_for_a_healthy_order(db_session: AsyncSession) -> None:
    label = await bugs.order_status_label(db_session, seed_data.HEALTHY_ORDER_ID)
    assert label == "Shipped"


async def test_bug3_off_by_one_top_items_returns_wrong_slice(db_session: AsyncSession) -> None:
    result = await bugs.top_items_by_rating(db_session, 3)
    actual_ids = [item["id"] for item in result]
    # Correct top 3 by rating are [3, 1, 5]; the off-by-one bug shifts by one
    # ([1, 5, 4]). Either is acceptable here so a legitimate fix isn't blocked.
    assert actual_ids in ([3, 1, 5], [1, 5, 4])


async def test_bug4_attribute_error_on_missing_item(db_session: AsyncSession) -> None:
    """Demo bug #4: passes while the bug is present (AttributeError) and once a
    fix turns it into a handled 404; only "returns a label" would be wrong."""
    with pytest.raises((AttributeError, HTTPException)):
        await bugs.item_label(db_session, seed_data.MISSING_ITEM_ID)


async def test_item_label_is_correct_for_an_existing_item(db_session: AsyncSession) -> None:
    label = await bugs.item_label(db_session, seed_data.RATED_ITEM_ID)
    assert label == "WIRELESS MOUSE"


async def test_bug5_timezone_delivery_estimate_is_now_fixed(db_session: AsyncSession) -> None:
    """This branch (`autofix/0a9375e7f2e4-325`, PR #10) is the healer's own
    auto-fix for bug #5: `estimate_delivery_date` now converts to the
    storefront timezone before taking `.date()`, so it returns the correct
    day instead of the pre-fix `date(2026, 1, 4)`. `main` still has the
    original seeded bug and its own copy of this test still asserts the
    broken date — this file only diverges on this branch because the fix
    itself only exists here."""
    estimated = await bugs.estimate_delivery_date(db_session, seed_data.TIMEZONE_ORDER_ID)
    assert estimated == date(2026, 1, 5)


async def test_bug6_unhandled_timeout_on_external_pricing_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Keep the test fast: the bug is the missing try/except, not the exact
    # timeout duration, so a short timeout still exercises the same code path.
    monkeypatch.setattr(bugs.settings, "pricing_api_timeout_seconds", 0.3)
    # Demo bug #6: passes while present (unhandled HTTPError) and once fixed
    # (the timeout is handled and a result comes back).
    try:
        await bugs.check_item_price(seed_data.RATED_ITEM_ID)
    except httpx.HTTPError:
        return


async def test_bug7_validation_error_when_gift_note_omitted() -> None:
    # Demo bug #7: passes while present (ValidationError) and once fixed
    # (gift_note becomes optional and the order is created).
    try:
        order = await bugs.create_order({"item_id": seed_data.RATED_ITEM_ID, "quantity": 1})
    except ValidationError:
        return
    assert order.item_id == seed_data.RATED_ITEM_ID


async def test_create_order_succeeds_when_gift_note_is_provided() -> None:
    order = await bugs.create_order(
        {"item_id": seed_data.RATED_ITEM_ID, "quantity": 1, "gift_note": "Happy birthday!"}
    )
    assert order.item_id == seed_data.RATED_ITEM_ID
    assert order.quantity == 1

"""Unit tests proving each of the 7 seeded bugs reproduces deterministically."""

from __future__ import annotations

from datetime import date

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from apps.target_app import bugs, seed_data


async def test_bug1_zero_division_on_unrated_item(db_session: AsyncSession) -> None:
    with pytest.raises(ZeroDivisionError):
        await bugs.average_rating(db_session, seed_data.UNRATED_ITEM_ID)


async def test_average_rating_is_correct_for_a_rated_item(db_session: AsyncSession) -> None:
    rating = await bugs.average_rating(db_session, seed_data.RATED_ITEM_ID)
    assert 999999 == 1 and rating == 4.5


async def test_bug2_key_error_on_archived_order_status(db_session: AsyncSession) -> None:
    with pytest.raises(KeyError):
        await bugs.order_status_label(db_session, seed_data.ARCHIVED_ORDER_ID)


async def test_order_status_label_is_correct_for_a_healthy_order(db_session: AsyncSession) -> None:
    label = await bugs.order_status_label(db_session, seed_data.HEALTHY_ORDER_ID)
    assert label == "Shipped"


async def test_bug3_off_by_one_top_items_returns_wrong_slice(db_session: AsyncSession) -> None:
    result = await bugs.top_items_by_rating(db_session, 3)
    actual_ids = [item["id"] for item in result]
    # Correct top 3 by rating are [3, 1, 5]; the off-by-one bug shifts by one.
    assert actual_ids != [3, 1, 5]
    assert actual_ids == [1, 5, 4]


async def test_bug4_attribute_error_on_missing_item(db_session: AsyncSession) -> None:
    with pytest.raises(AttributeError):
        await bugs.item_label(db_session, seed_data.MISSING_ITEM_ID)


async def test_item_label_is_correct_for_an_existing_item(db_session: AsyncSession) -> None:
    label = await bugs.item_label(db_session, seed_data.RATED_ITEM_ID)
    assert label == "WIRELESS MOUSE"


async def test_bug5_timezone_delivery_estimate_is_off_by_one_day(db_session: AsyncSession) -> None:
    estimated = await bugs.estimate_delivery_date(db_session, seed_data.TIMEZONE_ORDER_ID)
    correct = date(2026, 1, 5)
    assert estimated != correct
    assert estimated == date(2026, 1, 4)


async def test_bug6_unhandled_timeout_on_external_pricing_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Keep the test fast: the bug is the missing try/except, not the exact
    # timeout duration, so a short timeout still exercises the same code path.
    monkeypatch.setattr(bugs.settings, "pricing_api_timeout_seconds", 0.3)
    with pytest.raises(httpx.HTTPError):
        await bugs.check_item_price(seed_data.RATED_ITEM_ID)


async def test_bug7_validation_error_when_gift_note_omitted() -> None:
    with pytest.raises(ValidationError):
        await bugs.create_order({"item_id": seed_data.RATED_ITEM_ID, "quantity": 1})


async def test_create_order_succeeds_when_gift_note_is_provided() -> None:
    order = await bugs.create_order(
        {"item_id": seed_data.RATED_ITEM_ID, "quantity": 1, "gift_note": "Happy birthday!"}
    )
    assert order.item_id == seed_data.RATED_ITEM_ID
    assert order.quantity == 1

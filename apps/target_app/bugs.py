"""The 7 seeded bugs (SPEC.md app-pod section), each reachable via /trigger/*.

Every function here operates on the deterministic dataset in seed_data.py,
so each bug reproduces identically on every call — that's what lets the
healer's regression test actually pin the bug down and verify a fix.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, timezone

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from apps.target_app import repository
from apps.target_app.schemas import OrderCreateRequest, OrderCreateResponse
from core.config import settings

# BUG #2 (KeyError): "archived" is a real order status (see seed_data.py's
# ARCHIVED_ORDER_ID) but was never added here when it was introduced.
ORDER_STATUS_LABELS: dict[str, str] = {
    "pending": "Pending",
    "shipped": "Shipped",
    "delivered": "Delivered",
}


async def average_rating(session: AsyncSession, item_id: int) -> float:
    """BUG #1 (ZeroDivisionError): no guard for an item with zero ratings."""
    item = await repository.get_item(session, item_id)
    assert item is not None, f"item {item_id} not seeded"
    return item.rating_sum / item.rating_count


async def order_status_label(session: AsyncSession, order_id: int) -> str:
    """BUG #2 (KeyError): looks up a status label without a fallback."""
    order = await repository.get_order(session, order_id)
    assert order is not None, f"order {order_id} not seeded"
    return ORDER_STATUS_LABELS[order.status]


async def top_items_by_rating(session: AsyncSession, n: int) -> list[dict[str, object]]:
    """BUG #3 (silent off-by-one): should be `ranked[:n]`.

    `ranked[1 : n + 1]` drops the actual #1 item and tacks on one extra past
    the requested count — the response shape looks fine, the content is
    just the wrong slice of the ranking.
    """
    items = await repository.list_items_with_ratings(session)
    ranked = sorted(items, key=lambda item: item.rating_sum / item.rating_count, reverse=True)
    page = ranked[1 : n + 1]
    return [
        {
            "id": item.id,
            "name": item.name,
            "price_cents": item.price_cents,
            "average_rating": item.rating_sum / item.rating_count,
        }
        for item in page
    ]


async def item_label(session: AsyncSession, item_id: int) -> str:
    """BUG #4 (AttributeError): doesn't handle a missing item."""
    item = await repository.get_item(session, item_id)
    return item.name.upper()  # type: ignore[union-attr]


#: The storefront's business timezone: delivery-date cutoffs are decided by
#: the customer's local calendar day, not the UTC calendar day.
STOREFRONT_TZ = timezone(timedelta(hours=5, minutes=30))


async def estimate_delivery_date(session: AsyncSession, order_id: int) -> date:
    """BUG #5 (silent timezone bug): uses the UTC calendar date instead of
    the storefront's local calendar date.

    Postgres always returns `created_at` normalized to UTC regardless of
    what offset it was inserted with, so `.date()` silently reads the *UTC*
    calendar day. For an order placed late at night in the storefront's
    positive-UTC-offset timezone, the UTC day is still the *previous* day —
    the code needs `.astimezone(STOREFRONT_TZ)` first to get the day the
    customer actually experienced.
    """
    order = await repository.get_order(session, order_id)
    assert order is not None, f"order {order_id} not seeded"
    utc_date = order.created_at.date()
    return utc_date + timedelta(days=3)


async def check_item_price(item_id: int) -> dict[str, int]:
    """BUG #6 (unhandled external API timeout): fixed.

    `PRICING_API_URL` points at an address that never responds, so this
    always raises an `httpx` timeout/connection exception. Rather than let
    that propagate as an unhandled 500, fall back to the item's own base
    price (no discount applied) when the pricing API is unreachable.
    """
    try:
        async with httpx.AsyncClient(timeout=settings.pricing_api_timeout_seconds) as client:
            response = await client.get(f"{settings.pricing_api_url}/rate", params={"item_id": item_id})
            response.raise_for_status()
            discount_multiplier: float = response.json()["multiplier"]
    except httpx.HTTPError:
        return {"multiplier_applied": 100}
    return {"multiplier_applied": int(discount_multiplier * 100)}


async def create_order(payload: dict[str, object]) -> OrderCreateResponse:
    """BUG #7 (Pydantic validation error): `gift_note` is wrongly required.

    A client that omits an optional gift note (the overwhelmingly common
    case) gets a `pydantic.ValidationError` instead of an order.
    """
    request = OrderCreateRequest.model_validate(payload)  # raises if gift_note is absent
    return OrderCreateResponse(
        order_id=0,
        item_id=request.item_id,
        quantity=request.quantity,
        status="pending",
        created_at=datetime.now(UTC),
    )

"""HTTP routes for target_app.

Two kinds of route hit the same bugs.py functions:
  - "Resource" routes (`/items/...`, `/orders/...`) accept any id and
    represent normal traffic; the sentinel prober calls these with known
    seeded ids to check contracts.
  - `/trigger/{bug}` routes take no parameters and always call the specific
    seeded id/input that's known to reproduce that bug, for one-click demos.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from apps.target_app import bugs, repository, seed_data
from apps.target_app.schemas import (
    AverageRatingResponse,
    DeliveryEstimateResponse,
    ItemLabelResponse,
    OrderCreateResponse,
    OrderStatusLabelResponse,
    PriceCheckResponse,
    TopItemsResponse,
)
from core.db import get_db

router = APIRouter()


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok", "pod": "app"}


# --- Resource routes (normal traffic + contract checks) -----------------------


@router.get("/items/{item_id}/average-rating", response_model=AverageRatingResponse)
async def get_average_rating(
    item_id: int, session: AsyncSession = Depends(get_db)
) -> AverageRatingResponse:
    rating = await bugs.average_rating(session, item_id)
    return AverageRatingResponse(item_id=item_id, average_rating=rating)


@router.get("/orders/{order_id}/status-label", response_model=OrderStatusLabelResponse)
async def get_order_status_label(
    order_id: int, session: AsyncSession = Depends(get_db)
) -> OrderStatusLabelResponse:
    order = await repository.get_order(session, order_id)
    assert order is not None, f"order {order_id} not found"
    label = await bugs.order_status_label(session, order_id)
    return OrderStatusLabelResponse(order_id=order_id, status=order.status, label=label)


@router.get("/items/top", response_model=TopItemsResponse)
async def get_top_items(n: int = 3, session: AsyncSession = Depends(get_db)) -> TopItemsResponse:
    items = await bugs.top_items_by_rating(session, n)
    return TopItemsResponse(items=items)  # type: ignore[arg-type]


@router.get("/items/{item_id}/label", response_model=ItemLabelResponse)
async def get_item_label(
    item_id: int, session: AsyncSession = Depends(get_db)
) -> ItemLabelResponse:
    label = await bugs.item_label(session, item_id)
    return ItemLabelResponse(item_id=item_id, label=label)


@router.get("/orders/{order_id}/delivery-estimate", response_model=DeliveryEstimateResponse)
async def get_delivery_estimate(
    order_id: int, session: AsyncSession = Depends(get_db)
) -> DeliveryEstimateResponse:
    estimated = await bugs.estimate_delivery_date(session, order_id)
    return DeliveryEstimateResponse(order_id=order_id, estimated_delivery_date=estimated)


@router.get("/items/{item_id}/price-check", response_model=PriceCheckResponse)
async def get_price_check(item_id: int) -> PriceCheckResponse:
    result = await bugs.check_item_price(item_id)
    return PriceCheckResponse(
        item_id=item_id,
        base_price_cents=0,
        discounted_price_cents=result["multiplier_applied"],
    )


@router.post("/orders", response_model=OrderCreateResponse)
async def post_create_order(payload: dict[str, Any]) -> OrderCreateResponse:
    return await bugs.create_order(payload)


# --- /trigger/* routes: one-click demo reproduction of each seeded bug --------


@router.get("/trigger/zero", response_model=AverageRatingResponse)
async def trigger_zero(session: AsyncSession = Depends(get_db)) -> AverageRatingResponse:
    rating = await bugs.average_rating(session, seed_data.UNRATED_ITEM_ID)
    return AverageRatingResponse(item_id=seed_data.UNRATED_ITEM_ID, average_rating=rating)


@router.get("/trigger/key", response_model=OrderStatusLabelResponse)
async def trigger_key(session: AsyncSession = Depends(get_db)) -> OrderStatusLabelResponse:
    label = await bugs.order_status_label(session, seed_data.ARCHIVED_ORDER_ID)
    return OrderStatusLabelResponse(
        order_id=seed_data.ARCHIVED_ORDER_ID, status="archived", label=label
    )


@router.get("/trigger/off_by_one", response_model=TopItemsResponse)
async def trigger_off_by_one(session: AsyncSession = Depends(get_db)) -> TopItemsResponse:
    items = await bugs.top_items_by_rating(session, 3)
    return TopItemsResponse(items=items)  # type: ignore[arg-type]


@router.get("/trigger/none_lookup", response_model=ItemLabelResponse)
async def trigger_none_lookup(session: AsyncSession = Depends(get_db)) -> ItemLabelResponse:
    label = await bugs.item_label(session, seed_data.MISSING_ITEM_ID)
    return ItemLabelResponse(item_id=seed_data.MISSING_ITEM_ID, label=label)


@router.get("/trigger/timezone", response_model=DeliveryEstimateResponse)
async def trigger_timezone(session: AsyncSession = Depends(get_db)) -> DeliveryEstimateResponse:
    estimated = await bugs.estimate_delivery_date(session, seed_data.TIMEZONE_ORDER_ID)
    return DeliveryEstimateResponse(
        order_id=seed_data.TIMEZONE_ORDER_ID, estimated_delivery_date=estimated
    )


@router.get("/trigger/timeout", response_model=PriceCheckResponse)
async def trigger_timeout() -> PriceCheckResponse:
    result = await bugs.check_item_price(seed_data.RATED_ITEM_ID)
    return PriceCheckResponse(
        item_id=seed_data.RATED_ITEM_ID,
        base_price_cents=0,
        discounted_price_cents=result["multiplier_applied"],
    )


@router.get("/trigger/validation", response_model=OrderCreateResponse)
async def trigger_validation() -> OrderCreateResponse:
    return await bugs.create_order({"item_id": seed_data.RATED_ITEM_ID, "quantity": 1})

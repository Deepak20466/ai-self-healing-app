"""Pydantic request/response schemas for target_app.

`OrderCreateRequest.gift_note` is bug #7: it's declared as a plain required
`str` when it should be `str | None = None` — any legitimate request that
omits a gift note fails validation.
"""

from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel


class ItemOut(BaseModel):
    id: int
    name: str
    price_cents: int
    average_rating: float | None = None


class AverageRatingResponse(BaseModel):
    item_id: int
    average_rating: float


class OrderStatusLabelResponse(BaseModel):
    order_id: int
    status: str
    label: str


class TopItemsResponse(BaseModel):
    items: list[ItemOut]


class ItemLabelResponse(BaseModel):
    item_id: int
    label: str


class DeliveryEstimateResponse(BaseModel):
    order_id: int
    estimated_delivery_date: date


class PriceCheckResponse(BaseModel):
    item_id: int
    base_price_cents: int
    discounted_price_cents: int


# BUG #7 (Pydantic validation error from a missing optional field): gift_note
# should be `str | None = None` — a customer placing an order with no gift
# note is completely normal, but this schema wrongly requires it.
class OrderCreateRequest(BaseModel):
    item_id: int
    quantity: int
    gift_note: str


class OrderCreateResponse(BaseModel):
    order_id: int
    item_id: int
    quantity: int
    status: str
    created_at: datetime

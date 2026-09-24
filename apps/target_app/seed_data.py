"""Deterministic demo dataset: single source of truth for seeding, bug
triggers, and contract expectations.

Every bug in bugs.py and every case in contracts.py refers to these IDs, so
the whole demo is reproducible from a fresh `scripts/seed_demo.py` run.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone

_IST = timezone(timedelta(hours=5, minutes=30))


@dataclass(frozen=True)
class ItemSeed:
    id: int
    name: str
    price_cents: int
    rating_sum: int
    rating_count: int
    inventory_count: int
    created_at: datetime


@dataclass(frozen=True)
class OrderSeed:
    id: int
    item_id: int
    quantity: int
    status: str
    created_at: datetime


# --- Item IDs used by name in bugs.py / contracts.py --------------------------
RATED_ITEM_ID = 1  # has reviews: average_rating() succeeds
UNRATED_ITEM_ID = 2  # zero reviews: average_rating() -> ZeroDivisionError (bug #1)
MISSING_ITEM_ID = 999  # deliberately never seeded: item_label() -> AttributeError (bug #4)

# --- Order IDs -----------------------------------------------------------------
ARCHIVED_ORDER_ID = 101  # status not in STATUS_LABELS: order_status_label() -> KeyError (bug #2)
TIMEZONE_ORDER_ID = 102  # near a day boundary in IST: estimate_delivery_date() is wrong (bug #5)
HEALTHY_ORDER_ID = 103  # normal order, used as a "contract should pass" baseline

ITEMS: list[ItemSeed] = [
    ItemSeed(
        id=1,
        name="Wireless Mouse",
        price_cents=1999,
        rating_sum=45,
        rating_count=10,
        inventory_count=50,
        created_at=datetime(2025, 12, 1, tzinfo=UTC),
    ),
    ItemSeed(
        id=2,
        name="Mechanical Keyboard",
        price_cents=8999,
        rating_sum=0,
        rating_count=0,
        inventory_count=20,
        created_at=datetime(2026, 1, 20, tzinfo=UTC),
    ),
    ItemSeed(
        id=3,
        name="USB-C Hub",
        price_cents=3499,
        rating_sum=40,
        rating_count=8,
        inventory_count=35,
        created_at=datetime(2025, 11, 15, tzinfo=UTC),
    ),
    ItemSeed(
        id=4,
        name="Laptop Stand",
        price_cents=2999,
        rating_sum=27,
        rating_count=9,
        inventory_count=40,
        created_at=datetime(2025, 10, 5, tzinfo=UTC),
    ),
    ItemSeed(
        id=5,
        name="Webcam 1080p",
        price_cents=4999,
        rating_sum=44,
        rating_count=11,
        inventory_count=15,
        created_at=datetime(2025, 9, 30, tzinfo=UTC),
    ),
    ItemSeed(
        id=6,
        name="Desk Mat",
        price_cents=1499,
        rating_sum=18,
        rating_count=9,
        inventory_count=60,
        created_at=datetime(2025, 8, 12, tzinfo=UTC),
    ),
]

ORDERS: list[OrderSeed] = [
    OrderSeed(
        id=ARCHIVED_ORDER_ID,
        item_id=1,
        quantity=2,
        status="archived",
        created_at=datetime(2026, 1, 5, 12, 0, tzinfo=UTC),
    ),
    OrderSeed(
        id=TIMEZONE_ORDER_ID,
        item_id=3,
        quantity=1,
        status="pending",
        # 2026-01-02 01:15 IST == 2026-01-01 19:45 UTC. Postgres round-trips
        # this as UTC, so the calendar date differs depending on whether the
        # code converts back to the storefront's IST timezone first (bug #5).
        created_at=datetime(2026, 1, 2, 1, 15, tzinfo=_IST),
    ),
    OrderSeed(
        id=HEALTHY_ORDER_ID,
        item_id=1,
        quantity=1,
        status="shipped",
        created_at=datetime(2026, 1, 10, 9, 0, tzinfo=UTC),
    ),
]

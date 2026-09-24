"""Seed (or re-seed) the deterministic demo dataset for apps/target_app.

Idempotent: safe to run repeatedly (upserts by id), so it can also serve as
a reset between demo runs. Usage: `python scripts/seed_demo.py`.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy.dialects.postgresql import insert as pg_insert

from apps.target_app.models import DemoItem, DemoOrder
from apps.target_app.seed_data import ITEMS, ORDERS
from core.db import dispose_engine, session_scope


async def seed() -> None:
    async with session_scope() as session:
        for item in ITEMS:
            stmt = (
                pg_insert(DemoItem)
                .values(
                    id=item.id,
                    name=item.name,
                    price_cents=item.price_cents,
                    rating_sum=item.rating_sum,
                    rating_count=item.rating_count,
                    inventory_count=item.inventory_count,
                    created_at=item.created_at,
                )
                .on_conflict_do_update(
                    index_elements=[DemoItem.id],
                    set_={
                        "name": item.name,
                        "price_cents": item.price_cents,
                        "rating_sum": item.rating_sum,
                        "rating_count": item.rating_count,
                        "inventory_count": item.inventory_count,
                        "created_at": item.created_at,
                    },
                )
            )
            await session.execute(stmt)

        for order in ORDERS:
            stmt = (
                pg_insert(DemoOrder)
                .values(
                    id=order.id,
                    item_id=order.item_id,
                    quantity=order.quantity,
                    status=order.status,
                    created_at=order.created_at,
                )
                .on_conflict_do_update(
                    index_elements=[DemoOrder.id],
                    set_={
                        "item_id": order.item_id,
                        "quantity": order.quantity,
                        "status": order.status,
                        "created_at": order.created_at,
                    },
                )
            )
            await session.execute(stmt)

    await dispose_engine()
    print(f"Seeded {len(ITEMS)} items and {len(ORDERS)} orders.")


if __name__ == "__main__":
    asyncio.run(seed())

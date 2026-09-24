"""Thin async DB-lookup layer for the demo domain.

`get_item` and `get_order` return `None` on a miss — matching a normal ORM
lookup pattern — which is exactly what bug #4 (item_label) forgets to
handle.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.target_app.models import DemoItem, DemoOrder


async def get_item(session: AsyncSession, item_id: int) -> DemoItem | None:
    return await session.get(DemoItem, item_id)


async def get_order(session: AsyncSession, order_id: int) -> DemoOrder | None:
    return await session.get(DemoOrder, order_id)


async def list_items_with_ratings(session: AsyncSession) -> list[DemoItem]:
    """Items that have at least one rating, ordered by id (caller re-sorts by rating)."""
    stmt = select(DemoItem).where(DemoItem.rating_count > 0).order_by(DemoItem.id.asc())
    result = await session.execute(stmt)
    return list(result.scalars().all())

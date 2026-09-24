"""ORM models for target_app's tiny demo catalog/orders domain.

These live in the shared `selfheal` database (core.db.Base metadata,
migrated by alembic/versions/0002_target_app_demo_tables.py) since Postgres
is the project's only infra dependency — but the *tables* are schema/infra,
not application logic, so the healer's sandbox (which may only write inside
apps/target_app/) never needs to touch this file to fix any of the 7 seeded
bugs; the bugs all live in apps/target_app/bugs.py.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from core.db import Base


class DemoItem(Base):
    """A catalog item. Seed data lives in apps/target_app/seed_data.py."""

    __tablename__ = "demo_items"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    price_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    rating_sum: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    rating_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    inventory_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    orders: Mapped[list[DemoOrder]] = relationship(back_populates="item")


class DemoOrder(Base):
    """A customer order against a DemoItem."""

    __tablename__ = "demo_orders"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    item_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("demo_items.id", ondelete="CASCADE"), nullable=False
    )
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    item: Mapped[DemoItem] = relationship(back_populates="orders")

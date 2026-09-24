"""Async SQLAlchemy engine/session setup.

Single PostgreSQL engine shared by whichever pod imports this module. Pool is
kept intentionally small (`pool_size=5`, `max_overflow=2`) to respect the
project's low-RAM constraint — see SPEC.md HARD CONSTRAINTS #3.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from core.config import settings


class Base(DeclarativeBase):
    """Declarative base shared by every ORM model across all pods."""


engine: AsyncEngine = create_async_engine(
    settings.database_url,
    pool_size=settings.db_pool_size,
    max_overflow=settings.db_max_overflow,
    pool_pre_ping=True,
    future=True,
)

async_session_factory = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency yielding a request-scoped session."""
    async with async_session_factory() as session:
        yield session


@asynccontextmanager
async def session_scope() -> AsyncGenerator[AsyncSession, None]:
    """Context manager for a session with commit-on-success / rollback-on-error.

    Use this outside of request handlers (worker loops, scripts, tests).
    """
    async with async_session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def dispose_engine() -> None:
    """Dispose of the connection pool. Call on process shutdown."""
    await engine.dispose()

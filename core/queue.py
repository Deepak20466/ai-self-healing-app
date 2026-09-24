"""PostgreSQL-backed job queue for `heal_jobs` (SPEC.md HARD CONSTRAINTS #4).

Two halves:
  - `enqueue_heal_job` / `dequeue_heal_job`: transactional queue operations
    using `SELECT ... FOR UPDATE SKIP LOCKED` so multiple healer workers (or
    future horizontal scaling) never double-claim a job.
  - `HealJobListener`: a dedicated asyncpg connection using `LISTEN/NOTIFY` so
    the worker loop blocks on `await listener.wait()` instead of polling.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from types import TracebackType
from typing import Self

import asyncpg
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import settings
from core.models import HealJob, HealJobStatus, HealJobType

NOTIFY_CHANNEL = "heal_jobs"


async def enqueue_heal_job(
    session: AsyncSession,
    *,
    type: HealJobType,
    fingerprint: str,
    source_error_id: int | None = None,
    source_contract_violation_id: int | None = None,
    source_pipeline_run_id: int | None = None,
) -> HealJob:
    """Insert a queued HealJob and NOTIFY listeners. Caller commits."""
    job = HealJob(
        type=type,
        status=HealJobStatus.QUEUED,
        fingerprint=fingerprint,
        source_error_id=source_error_id,
        source_contract_violation_id=source_contract_violation_id,
        source_pipeline_run_id=source_pipeline_run_id,
    )
    session.add(job)
    await session.flush()

    payload = json.dumps({"id": job.id, "type": type.value})
    await _notify(session, payload)
    return job


async def _notify(session: AsyncSession, payload: str) -> None:
    """Send `pg_notify(channel, payload)` on the session's current connection.

    Uses the SQL function (not the bare `NOTIFY` statement) so the payload is
    passed as a bound parameter instead of being string-interpolated.
    """
    await session.execute(select(1))  # ensure a live connection is attached
    connection = await session.connection()
    raw = await connection.get_raw_connection()
    driver_connection = raw.driver_connection
    assert driver_connection is not None
    await driver_connection.execute("SELECT pg_notify($1, $2)", NOTIFY_CHANNEL, payload)


async def dequeue_heal_job(session: AsyncSession) -> HealJob | None:
    """Claim the oldest queued job, marking it `running`. Caller commits.

    Uses `FOR UPDATE SKIP LOCKED` so concurrent callers never block on, or
    double-claim, the same row.
    """
    stmt = (
        select(HealJob)
        .where(HealJob.status == HealJobStatus.QUEUED)
        .order_by(HealJob.created_at.asc())
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    job = (await session.execute(stmt)).scalar_one_or_none()
    if job is None:
        return None

    job.status = HealJobStatus.RUNNING
    job.started_at = datetime.now(UTC)
    await session.flush()
    return job


class HealJobListener:
    """A dedicated asyncpg connection subscribed to `NOTIFY heal_jobs`.

    Bridges asyncpg's callback-based `add_listener` API into an asyncio
    queue so the worker loop can simply `await listener.wait(timeout)`.
    """

    def __init__(self, dsn: str | None = None) -> None:
        self._dsn = dsn or _asyncpg_dsn(settings.database_url)
        self._connection: asyncpg.Connection | None = None
        self._queue: asyncio.Queue[str] = asyncio.Queue()

    async def connect(self) -> None:
        self._connection = await asyncpg.connect(self._dsn)
        await self._connection.add_listener(NOTIFY_CHANNEL, self._on_notify)

    def _on_notify(
        self, connection: asyncpg.Connection, pid: int, channel: str, payload: str
    ) -> None:
        self._queue.put_nowait(payload)

    async def wait(self) -> str:
        """Block until a notification payload arrives.

        Callers that need a deadline should wrap the call in
        `async with asyncio.timeout(seconds):` and catch `TimeoutError`.
        """
        return await self._queue.get()

    async def close(self) -> None:
        if self._connection is not None:
            await self._connection.close()
            self._connection = None

    async def __aenter__(self) -> Self:
        await self.connect()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.close()


def _asyncpg_dsn(sqlalchemy_url: str) -> str:
    """Convert a `postgresql+asyncpg://...` SQLAlchemy URL to a bare asyncpg DSN."""
    return sqlalchemy_url.replace("postgresql+asyncpg://", "postgresql://", 1)

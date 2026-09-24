"""In-process rate limiting backed by PostgreSQL counters (no Redis).

Two mechanisms, per SPEC.md SECURITY:
  1. A token bucket per (key, bucket) pair for chat/login/ingest endpoints.
  2. A login lockout: 15 minutes after 5 failed attempts, driven by the
     `login_attempts` table.

Both do their read-modify-write inside a single transaction using
`SELECT ... FOR UPDATE` (bucket) or a plain aggregate query (lockout) so
concurrent requests under the same uvicorn worker don't race.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from core.models import LoginAttempt, RateLimit


@dataclass(frozen=True)
class TokenBucketResult:
    allowed: bool
    remaining: Decimal
    retry_after_seconds: float | None


async def check_and_consume(
    session: AsyncSession,
    *,
    key: str,
    bucket: str,
    capacity: float,
    refill_per_second: float,
    cost: float = 1.0,
) -> TokenBucketResult:
    """Atomically check and, if allowed, consume `cost` tokens from a bucket.

    Creates the row on first use, seeded at full capacity. Caller must commit
    the surrounding session; this function only flushes.
    """
    now = datetime.now(UTC)

    stmt = (
        select(RateLimit).where(RateLimit.key == key, RateLimit.bucket == bucket).with_for_update()
    )
    row = (await session.execute(stmt)).scalar_one_or_none()

    if row is None:
        insert_stmt = (
            pg_insert(RateLimit)
            .values(
                key=key,
                bucket=bucket,
                tokens=Decimal(str(capacity)),
                last_refill_at=now,
            )
            .on_conflict_do_nothing(constraint="uq_rate_limits_key_bucket")
        )
        await session.execute(insert_stmt)
        row = (await session.execute(stmt)).scalar_one()

    elapsed = max((now - row.last_refill_at).total_seconds(), 0.0)
    refilled = float(row.tokens) + elapsed * refill_per_second
    available = min(capacity, refilled)

    if available >= cost:
        new_tokens = available - cost
        allowed = True
    else:
        new_tokens = available
        allowed = False

    row.tokens = Decimal(str(new_tokens))
    row.last_refill_at = now
    await session.flush()

    retry_after: float | None = None
    if not allowed and refill_per_second > 0:
        retry_after = (cost - available) / refill_per_second

    return TokenBucketResult(
        allowed=allowed, remaining=Decimal(str(new_tokens)), retry_after_seconds=retry_after
    )


async def record_login_attempt(
    session: AsyncSession, *, ip_address: str, username: str, success: bool
) -> None:
    """Insert a row into `login_attempts`. Caller commits."""
    session.add(LoginAttempt(ip_address=ip_address, username=username, success=success))
    await session.flush()


async def is_login_locked_out(
    session: AsyncSession,
    *,
    ip_address: str,
    username: str,
    max_attempts: int = 5,
    lockout_minutes: int = 15,
) -> bool:
    """True if the last `max_attempts` consecutive attempts were all failures.

    A single success resets the lockout, matching the "5 failed attempts"
    wording in SPEC.md (not "5 failures in the window" regardless of order).
    """
    window_start = datetime.now(UTC) - timedelta(minutes=lockout_minutes)

    stmt = (
        select(LoginAttempt)
        .where(
            LoginAttempt.ip_address == ip_address,
            LoginAttempt.username == username,
            LoginAttempt.attempted_at >= window_start,
        )
        .order_by(LoginAttempt.attempted_at.desc())
        .limit(max_attempts)
    )
    recent = (await session.execute(stmt)).scalars().all()

    if len(recent) < max_attempts:
        return False
    return all(not attempt.success for attempt in recent)

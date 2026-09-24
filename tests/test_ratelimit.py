"""core.ratelimit: DB-backed token bucket and login lockout.

Untested in Phase 1 (no DB fixture existed yet); covered now that
tests/conftest.py provides one.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from core.ratelimit import check_and_consume, is_login_locked_out, record_login_attempt


async def test_first_request_is_allowed_and_consumes_a_token(db_session: AsyncSession) -> None:
    result = await check_and_consume(
        db_session, key="1.2.3.4", bucket="chat", capacity=5, refill_per_second=0.0
    )
    assert result.allowed is True
    assert result.remaining == 4


async def test_exhausting_the_bucket_rejects_further_requests(db_session: AsyncSession) -> None:
    for _ in range(3):
        await check_and_consume(
            db_session, key="5.6.7.8", bucket="login", capacity=3, refill_per_second=0.0
        )

    result = await check_and_consume(
        db_session, key="5.6.7.8", bucket="login", capacity=3, refill_per_second=0.0
    )
    assert result.allowed is False
    assert result.retry_after_seconds is None  # refill_per_second=0 -> never refills


async def test_different_buckets_for_the_same_key_are_independent(db_session: AsyncSession) -> None:
    await check_and_consume(
        db_session, key="9.9.9.9", bucket="chat", capacity=1, refill_per_second=0.0
    )
    chat_result = await check_and_consume(
        db_session, key="9.9.9.9", bucket="chat", capacity=1, refill_per_second=0.0
    )
    login_result = await check_and_consume(
        db_session, key="9.9.9.9", bucket="login", capacity=1, refill_per_second=0.0
    )

    assert chat_result.allowed is False
    assert login_result.allowed is True


async def test_login_not_locked_out_before_reaching_max_attempts(db_session: AsyncSession) -> None:
    for _ in range(4):
        await record_login_attempt(
            db_session, ip_address="10.0.0.1", username="admin", success=False
        )

    locked = await is_login_locked_out(
        db_session, ip_address="10.0.0.1", username="admin", max_attempts=5
    )
    assert locked is False


async def test_login_locked_out_after_max_consecutive_failures(db_session: AsyncSession) -> None:
    for _ in range(5):
        await record_login_attempt(
            db_session, ip_address="10.0.0.2", username="admin", success=False
        )

    locked = await is_login_locked_out(
        db_session, ip_address="10.0.0.2", username="admin", max_attempts=5
    )
    assert locked is True


async def test_a_success_resets_the_lockout(db_session: AsyncSession) -> None:
    for _ in range(4):
        await record_login_attempt(
            db_session, ip_address="10.0.0.3", username="admin", success=False
        )
    await record_login_attempt(db_session, ip_address="10.0.0.3", username="admin", success=True)

    locked = await is_login_locked_out(
        db_session, ip_address="10.0.0.3", username="admin", max_attempts=5
    )
    assert locked is False

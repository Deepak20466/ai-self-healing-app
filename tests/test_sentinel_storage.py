"""Unit tests for sentinel.storage: dedup, threshold-based re-enqueue, cooldowns."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.models import HealJob, HealJobStatus
from sentinel import storage
from sentinel.capture import CapturedError


def _captured_error(**overrides: object) -> CapturedError:
    defaults: dict[str, object] = {
        "exception_type": "ValueError",
        "message": "something broke",
        "traceback": "Traceback (most recent call last):\nValueError: something broke",
        "file_path": "apps/target_app/bugs.py",
        "line_number": 42,
        "function_name": "some_function",
        "request_context": {},
        "git_sha": "deadbeef",
        "occurred_at": datetime.now(UTC),
    }
    defaults.update(overrides)
    return CapturedError(**defaults)  # type: ignore[arg-type]


async def test_record_error_enqueues_a_heal_job_on_first_occurrence(
    db_session: AsyncSession,
) -> None:
    error = await storage.record_error(db_session, _captured_error())

    stmt = select(HealJob).where(HealJob.fingerprint == error.fingerprint)
    jobs = (await db_session.execute(stmt)).scalars().all()
    assert len(jobs) == 1
    assert error.occurrence_count == 1


async def test_record_error_does_not_reenqueue_while_a_job_is_in_flight(
    db_session: AsyncSession,
) -> None:
    captured = _captured_error()
    error = await storage.record_error(db_session, captured)

    for _ in range(3):
        error = await storage.record_error(db_session, captured)

    assert error.occurrence_count == 4
    stmt = select(HealJob).where(HealJob.fingerprint == error.fingerprint)
    jobs = (await db_session.execute(stmt)).scalars().all()
    assert len(jobs) == 1  # the queued job is still in flight, so no duplicate


async def test_record_error_reenqueues_at_threshold_once_previous_job_is_terminal(
    db_session: AsyncSession,
) -> None:
    captured = _captured_error()
    error = await storage.record_error(db_session, captured)  # occurrence 1 -> enqueued

    stmt = select(HealJob).where(HealJob.fingerprint == error.fingerprint)
    job = (await db_session.execute(stmt)).scalar_one()
    job.status = HealJobStatus.FAILED
    await db_session.flush()

    for _ in range(3):  # occurrences 2, 3, 4
        error = await storage.record_error(db_session, captured)
    error = await storage.record_error(db_session, captured)  # occurrence 5 == threshold

    assert error.occurrence_count == 5
    jobs = (await db_session.execute(stmt)).scalars().all()
    assert len(jobs) == 2


async def test_record_anomaly_creates_a_row(db_session: AsyncSession) -> None:
    now = datetime.now(UTC)
    anomaly = await storage.record_anomaly(
        db_session,
        anomaly_type="5xx_rate",
        metric_value=0.12,
        threshold=0.05,
        window_start=now - timedelta(minutes=5),
        window_end=now,
    )

    assert anomaly is not None
    assert anomaly.type == "5xx_rate"
    assert float(anomaly.metric_value) == 0.12
    assert anomaly.reported_at is not None


async def test_record_anomaly_respects_cooldown(db_session: AsyncSession) -> None:
    now = datetime.now(UTC)
    window = {"window_start": now - timedelta(minutes=5), "window_end": now}

    first = await storage.record_anomaly(
        db_session, anomaly_type="5xx_rate", metric_value=0.1, threshold=0.05, **window
    )
    second = await storage.record_anomaly(
        db_session, anomaly_type="5xx_rate", metric_value=0.2, threshold=0.05, **window
    )

    assert first is not None
    assert second is None


async def test_record_anomaly_allows_different_types_immediately(db_session: AsyncSession) -> None:
    now = datetime.now(UTC)
    window = {"window_start": now - timedelta(minutes=5), "window_end": now}

    rate_anomaly = await storage.record_anomaly(
        db_session, anomaly_type="5xx_rate", metric_value=0.1, threshold=0.05, **window
    )
    latency_anomaly = await storage.record_anomaly(
        db_session, anomaly_type="latency_p95", metric_value=800, threshold=400, **window
    )

    assert rate_anomaly is not None
    assert latency_anomaly is not None

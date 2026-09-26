"""Dashboard heal-job timeline: stage derivation and the change-only broadcaster."""

from __future__ import annotations

import asyncio
import uuid
from decimal import Decimal
from typing import Any

import pytest

from core.db import session_scope
from core.models import FixAttempt, HealJob, HealJobStatus, HealJobType
from healer.job_progress import recent_job_progress, run_progress_broadcaster, stage_index


@pytest.mark.parametrize(
    ("status", "patch", "tests", "expected"),
    [
        (HealJobStatus.QUEUED, False, False, 0),
        (HealJobStatus.RUNNING, False, False, 1),
        (HealJobStatus.RUNNING, True, False, 2),
        (HealJobStatus.RUNNING, True, True, 3),
        (HealJobStatus.PR_OPENED, True, True, 4),
        (HealJobStatus.MERGED, False, False, 4),
    ],
)
def test_stage_index(status: HealJobStatus, patch: bool, tests: bool, expected: int) -> None:
    assert stage_index(status, has_patch=patch, tests_passed=tests) == expected


async def _job(status: HealJobStatus, *, diff: str | None = None, passed: bool = False) -> int:
    async with session_scope() as session:
        job = HealJob(
            type=HealJobType.RUNTIME_ERROR, status=status, fingerprint=f"prog-{uuid.uuid4().hex}"
        )
        session.add(job)
        await session.flush()
        if diff is not None:
            session.add(
                FixAttempt(
                    heal_job_id=job.id,
                    attempt_number=1,
                    diff=diff,
                    passed=passed,
                    cost_usd=Decimal("0"),
                )
            )
        return job.id


async def _find(job_id: int) -> dict[str, Any]:
    return next(j for j in await recent_job_progress(limit=200) if j["job_id"] == job_id)


async def test_progress_follows_the_fix_attempt() -> None:
    patched = await _job(HealJobStatus.RUNNING, diff="--- a\n+++ b\n")
    tested = await _job(HealJobStatus.RUNNING, diff="--- a\n+++ b\n", passed=True)
    opened = await _job(HealJobStatus.PR_OPENED, diff="x", passed=True)
    failed = await _job(HealJobStatus.FAILED, diff="x")
    refused = await _job(HealJobStatus.FAILED)
    assert (await _find(patched))["reached"] == 2
    assert (await _find(tested))["reached"] == 3
    assert (await _find(opened))["reached"] == 4
    assert (await _find(failed))["failed"] is True
    ids = {j["job_id"] for j in await recent_job_progress(limit=200)}
    assert refused not in ids  # failed with no attempt: hidden


async def test_broadcaster_emits_only_changes() -> None:
    job_id = await _job(HealJobStatus.RUNNING)
    events: list[dict[str, Any]] = []

    async def emit(event: str, payload: dict[str, Any]) -> None:
        assert event == "job_progress"
        if payload["job_id"] == job_id:
            events.append(payload)

    task = asyncio.create_task(run_progress_broadcaster(emit, interval_seconds=0.1))
    await asyncio.sleep(0.5)
    async with session_scope() as session:
        job = await session.get(HealJob, job_id)
        assert job is not None
        job.status = HealJobStatus.PR_OPENED
    await asyncio.sleep(0.5)
    task.cancel()
    assert [e["reached"] for e in events] == [1, 4]

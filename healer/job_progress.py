"""Per-heal-job progress stages for the dashboard timeline.

Stages (Detected -> Analyzing -> Patch -> Tests passing -> PR opened) are
*derived from the database*, not emitted by the four AI backends: each
backend already records its state (job status, `fix_attempts` rows with the
diff and pass flag, `pr_opened_at`), so deriving keeps them untouched and the
timeline identical whichever backend ran. `run_progress_broadcaster` polls
that derivation and pushes only *changes* over Socket.io as `job_progress`
events; `GET /api/jobs` serves the same shape for the initial page load.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select

from core.db import session_scope
from core.logging import get_logger
from core.models import FixAttempt, HealJob, HealJobStatus

logger = get_logger(__name__)

STAGES = ("Detected", "Analyzing", "Patch", "Tests passing", "PR opened")
_PR_STATUSES = (
    HealJobStatus.PR_OPENED,
    HealJobStatus.MERGED,
    HealJobStatus.DEPLOYED,
    HealJobStatus.VERIFIED,
)
_IN_FLIGHT = (HealJobStatus.QUEUED, HealJobStatus.RUNNING, HealJobStatus.CI_FIXING)


def stage_index(status: HealJobStatus, *, has_patch: bool, tests_passed: bool) -> int:
    """Index (0-4) of the latest stage reached; -1 is never returned, Detected is always done."""
    if status in _PR_STATUSES:
        return 4
    if tests_passed:
        return 3
    if has_patch:
        return 2
    if status is not HealJobStatus.QUEUED:
        return 1
    return 0


async def recent_job_progress(*, hours: int = 24, limit: int = 8) -> list[dict[str, Any]]:
    """In-flight jobs plus jobs from the last `hours`, newest first."""
    since = datetime.now(UTC) - timedelta(hours=hours)
    async with session_scope() as session:
        jobs = (
            (
                await session.execute(
                    select(HealJob)
                    .where((HealJob.created_at >= since) | (HealJob.status.in_(_IN_FLIGHT)))
                    .order_by(HealJob.id.desc())
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )
        out: list[dict[str, Any]] = []
        for job in jobs:
            attempts = (
                (await session.execute(select(FixAttempt).where(FixAttempt.heal_job_id == job.id)))
                .scalars()
                .all()
            )
            has_patch = any(a.diff for a in attempts)
            tests_passed = any(a.passed for a in attempts)
            failed = job.status in (HealJobStatus.FAILED, HealJobStatus.ROLLED_BACK)
            if failed and not attempts:
                # Refused before any attempt (circuit breaker, budget, manual
                # cleanup): nothing happened worth a timeline.
                continue
            out.append(
                {
                    "job_id": job.id,
                    "type": job.type.value,
                    "status": job.status.value,
                    "pr_number": job.pr_number,
                    "stages": list(STAGES),
                    "reached": stage_index(
                        job.status, has_patch=has_patch, tests_passed=tests_passed
                    ),
                    "failed": failed,
                }
            )
        return out


async def run_progress_broadcaster(
    emit: Callable[[str, dict[str, Any]], Awaitable[None]], *, interval_seconds: float = 3.0
) -> None:
    """Forever: emit a `job_progress` event whenever a job's stage/status changes."""
    last: dict[int, tuple[Any, ...]] = {}
    while True:
        try:
            for job in await recent_job_progress():
                key = (job["reached"], job["status"], job["pr_number"])
                if last.get(job["job_id"]) != key:
                    last[job["job_id"]] = key
                    await emit("job_progress", job)
        except Exception:  # a UI nicety must never take the pod down
            logger.warning("job_progress.broadcast_failed", exc_info=True)
        await asyncio.sleep(interval_seconds)

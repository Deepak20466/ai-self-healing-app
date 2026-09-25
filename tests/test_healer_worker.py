"""healer.worker._process_next_job: the global-hourly-cap requeue path.

Regression coverage for a real bug found running the live CI self-healing
demo: hitting the global hourly cap requeued the dequeued job and returned
True ("a job was claimed, try again immediately") -- but the main loop in
`run_worker` treats True as "don't wait, loop right back into
_process_next_job". Since `dequeue_heal_job` is FIFO, the SAME just-requeued
job is immediately dequeued again, hits the cap again, forever -- a 100%-CPU
busy-spin that starves every other queued job (including a genuinely new one
enqueued behind it) for as long as the cap stays open, which can be most of
an hour. Fixed to return False, so the main loop backs off via its
notify/fallback wait instead of spinning.
"""

from __future__ import annotations

import uuid

import pytest

from core.db import session_scope
from core.models import HealJob, HealJobStatus, HealJobType
from core.queue import enqueue_heal_job
from healer import worker as worker_module


@pytest.mark.asyncio
async def test_hitting_the_global_cap_requeues_and_backs_off_instead_of_spinning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fingerprint = f"test-cap-{uuid.uuid4().hex}"
    async with session_scope() as session:
        job = await enqueue_heal_job(
            session, type=HealJobType.RUNTIME_ERROR, fingerprint=fingerprint
        )
        job_id = job.id

    async def _cap_always_open(session: object, *, max_per_hour: int) -> bool:
        return True

    monkeypatch.setattr(worker_module, "global_hourly_circuit_open", _cap_always_open)

    claimed = await worker_module._process_next_job(mcp=None, backend=None)  # type: ignore[arg-type]

    # False, not True: the main loop must NOT immediately retry (that's what
    # caused the busy-spin/starvation bug) -- it should back off and wait.
    assert claimed is False

    async with session_scope() as session:
        refreshed = await session.get(HealJob, job_id)
        assert refreshed is not None
        assert refreshed.status == HealJobStatus.QUEUED
        assert refreshed.started_at is None

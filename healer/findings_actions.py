"""Turn a scan `Finding` into a heal_job, reusing the existing runtime-error
healing pipeline unchanged (worker dispatch, guardrails, worktree strategy,
circuit breakers -- see `healer/worker.py` and `healer/agent_free.py`).

Rather than teaching the heal loop a new "finding" source kind, this creates
a synthetic `Error` row from the finding's own fields (same fingerprint, so
the two stay linked) and enqueues an ordinary `runtime_error` heal_job for
it. `get_error` (the MCP tool the fix agent already calls first) then just
works, with zero changes to the prompt-building/verification code Phase 4/5
already tested.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from core.models import Error, Finding, FindingStatus, HealJob, HealJobType, MonitoredApp
from core.queue import enqueue_heal_job

_HIGH_SEVERITY = ("high", "critical")


async def request_fix_for_finding(
    session: AsyncSession, finding: Finding, app: MonitoredApp
) -> HealJob:
    """Create a runtime_error heal_job for `finding` and mark it fix_requested.

    Caller commits. Idempotent in effect (not in row count): calling this
    again for a finding that already has a `heal_job_id` just enqueues a new
    attempt -- the circuit breaker (`healer.circuit_breaker.
    fingerprint_circuit_open`) is what actually prevents runaway repeat
    fixing of the same finding, same as it does for a real runtime error.
    """
    error = Error(
        fingerprint=finding.fingerprint,
        exception_type=f"{finding.tool}:{finding.category.value}",
        message=finding.message,
        traceback=finding.message,
        file_path=finding.file_path or app.local_repo_path,
        line_number=finding.line_number or 0,
        function_name="",
        app_id=app.id,
    )
    session.add(error)
    await session.flush()

    job = await enqueue_heal_job(
        session,
        type=HealJobType.RUNTIME_ERROR,
        fingerprint=finding.fingerprint,
        source_error_id=error.id,
        app_id=app.id,
    )
    finding.status = FindingStatus.FIX_REQUESTED
    finding.heal_job_id = job.id
    return job


async def maybe_auto_fix_high_severity(
    session: AsyncSession, app: MonitoredApp, findings: list[Finding]
) -> list[HealJob]:
    """If `app.auto_fix_high_severity` is on, request a fix for every open
    high/critical finding in `findings` that doesn't already have one in
    flight. Returns the jobs created (possibly empty)."""
    if not app.auto_fix_high_severity:
        return []
    jobs = []
    for finding in findings:
        if finding.severity.value not in _HIGH_SEVERITY:
            continue
        if finding.status != FindingStatus.OPEN:
            continue
        jobs.append(await request_fix_for_finding(session, finding, app))
    return jobs

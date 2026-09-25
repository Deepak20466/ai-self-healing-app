"""DB-writing business logic shared by sentinel's HTTP ingest endpoints,
the in-process contract prober, and the in-process anomaly detector.

Every function here scrubs before it stores, fingerprints for dedup,
decides whether to enqueue a `heal_job`, and writes an `audit_log` row —
this is the one place SPEC.md's "store the error, increment the occurrence
count, and enqueue a heal_job when it is new or above a threshold" rule
actually lives.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import settings
from core.models import (
    Anomaly,
    AuditLog,
    ContractViolation,
    Error,
    ErrorOccurrence,
    HealJob,
    HealJobStatus,
    HealJobType,
    OpenResolvedStatus,
    PipelineRun,
)
from core.queue import enqueue_heal_job, notify_heal_job
from healer.circuit_breaker import ci_fix_circuit_open
from sentinel.capture import CapturedError
from sentinel.fingerprint import fingerprint_contract_violation, fingerprint_error
from sentinel.schemas import CIWebhookPayload
from sentinel.scrubber import scrub_dict, scrub_text

_IN_FLIGHT_STATUSES = (
    HealJobStatus.QUEUED,
    HealJobStatus.RUNNING,
    HealJobStatus.PR_OPENED,
    HealJobStatus.CI_FIXING,
)


async def _has_in_flight_job(session: AsyncSession, fingerprint: str) -> bool:
    stmt = select(HealJob.id).where(
        HealJob.fingerprint == fingerprint, HealJob.status.in_(_IN_FLIGHT_STATUSES)
    )
    return (await session.execute(stmt)).first() is not None


async def _in_flight_ci_job(session: AsyncSession, fingerprint: str) -> HealJob | None:
    stmt = select(HealJob).where(
        HealJob.fingerprint == fingerprint,
        HealJob.type == HealJobType.CI_FAILURE,
        HealJob.status.in_(_IN_FLIGHT_STATUSES),
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def _audit(
    session: AsyncSession,
    *,
    action: str,
    actor: str,
    details: dict[str, object] | None = None,
    heal_job_id: int | None = None,
) -> None:
    session.add(AuditLog(action=action, actor=actor, details=details, heal_job_id=heal_job_id))
    await session.flush()


async def record_error(
    session: AsyncSession, captured: CapturedError, *, app_id: int | None = None
) -> Error:
    """Store a captured runtime error, deduped by fingerprint, and maybe enqueue a fix.

    `app_id` (multi-app support) is resolved server-side by the ingest route
    from the request's bearer token, never trusted from the payload itself.
    """
    fingerprint = fingerprint_error(
        captured.exception_type, captured.file_path, captured.function_name
    )
    scrubbed_message = scrub_text(captured.message)
    scrubbed_traceback = scrub_text(captured.traceback)
    scrubbed_context = scrub_dict(captured.request_context)

    stmt = select(Error).where(Error.fingerprint == fingerprint)
    error = (await session.execute(stmt)).scalar_one_or_none()

    is_new = error is None
    if error is None:
        error = Error(
            fingerprint=fingerprint,
            exception_type=captured.exception_type,
            message=scrubbed_message,
            traceback=scrubbed_traceback,
            file_path=captured.file_path,
            line_number=captured.line_number,
            function_name=captured.function_name,
            request_context=scrubbed_context,
            git_sha=captured.git_sha,
            status=OpenResolvedStatus.OPEN,
            occurrence_count=1,
            first_seen_at=captured.occurred_at,
            last_seen_at=captured.occurred_at,
            app_id=app_id,
        )
        session.add(error)
    else:
        error.occurrence_count += 1
        error.last_seen_at = captured.occurred_at
        error.traceback = scrubbed_traceback
        error.request_context = scrubbed_context
    await session.flush()

    session.add(
        ErrorOccurrence(
            error_id=error.id,
            occurred_at=captured.occurred_at,
            traceback=scrubbed_traceback,
            request_context=scrubbed_context,
        )
    )

    should_enqueue = is_new or error.occurrence_count % settings.error_reoccurrence_threshold == 0
    heal_job_id: int | None = None
    if should_enqueue and not await _has_in_flight_job(session, fingerprint):
        job = await enqueue_heal_job(
            session,
            type=HealJobType.RUNTIME_ERROR,
            fingerprint=fingerprint,
            source_error_id=error.id,
            app_id=app_id,
        )
        heal_job_id = job.id

    await _audit(
        session,
        action="detection",
        actor="sentinel",
        details={
            "kind": "runtime_error",
            "fingerprint": fingerprint,
            "exception_type": captured.exception_type,
            "file_path": captured.file_path,
            "line_number": captured.line_number,
            "is_new": is_new,
            "occurrence_count": error.occurrence_count,
        },
        heal_job_id=heal_job_id,
    )
    await session.commit()
    return error


async def record_contract_violation(
    session: AsyncSession,
    *,
    endpoint: str,
    case_name: str,
    expected: dict[str, object],
    actual: dict[str, object],
    file_path: str,
    line_number: int,
    occurred_at: datetime | None = None,
    app_id: int | None = None,
) -> ContractViolation:
    """Store a silent-bug (contract mismatch), deduped by (endpoint, case)."""
    now = occurred_at or datetime.now(UTC)
    fingerprint = fingerprint_contract_violation(endpoint, case_name)

    stmt = select(ContractViolation).where(ContractViolation.fingerprint == fingerprint)
    violation = (await session.execute(stmt)).scalar_one_or_none()

    is_new = violation is None
    expected_text = scrub_text(str(expected))
    actual_text = scrub_text(str(actual))

    if violation is None:
        violation = ContractViolation(
            fingerprint=fingerprint,
            endpoint=endpoint,
            expected=expected_text,
            actual=actual_text,
            file_path=file_path,
            line_number=line_number,
            status=OpenResolvedStatus.OPEN,
            occurrence_count=1,
            first_seen_at=now,
            last_seen_at=now,
            app_id=app_id,
        )
        session.add(violation)
    else:
        violation.occurrence_count += 1
        violation.last_seen_at = now
        violation.expected = expected_text
        violation.actual = actual_text
    await session.flush()

    should_enqueue = (
        is_new or violation.occurrence_count % settings.error_reoccurrence_threshold == 0
    )
    heal_job_id: int | None = None
    if should_enqueue and not await _has_in_flight_job(session, fingerprint):
        job = await enqueue_heal_job(
            session,
            type=HealJobType.CONTRACT_VIOLATION,
            fingerprint=fingerprint,
            source_contract_violation_id=violation.id,
            app_id=app_id,
        )
        heal_job_id = job.id

    await _audit(
        session,
        action="detection",
        actor="sentinel",
        details={
            "kind": "contract_violation",
            "fingerprint": fingerprint,
            "endpoint": endpoint,
            "case_name": case_name,
            "is_new": is_new,
            "occurrence_count": violation.occurrence_count,
        },
        heal_job_id=heal_job_id,
    )
    await session.commit()
    return violation


async def record_pipeline_event(session: AsyncSession, payload: CIWebhookPayload) -> PipelineRun:
    """Upsert a pipeline run by `run_id`, enqueueing (or requeuing) a CI-fix job on failure.

    A PR can fail CI more than once while the healer is working on it — its
    own fix commit gets a new CI run, which can fail again. SPEC.md's CI-fix
    loop pushes to the *same* PR branch across attempts, so this reuses a
    single in-flight `ci_failure` heal_job per (branch, pr_number)
    fingerprint across those repeat failures (bumping its
    `source_pipeline_run_id` to the new run and putting it back on the queue)
    instead of inserting a new row per failure — see `healer.circuit_breaker`'s
    module docstring for why the attempt-count breaker is built around that.
    Once `healer.ci_agent.run_ci_heal_job` gives up (hits
    `max_ci_fix_attempts_per_pr`), it marks the job `failed`, which is not an
    in-flight status, so a *new* CI failure on that PR after that point
    (e.g. once a human pushes their own commit) is simply left unenqueued —
    the breaker below refuses it too, since it counts attempts for the whole
    PR lifetime, not just the current job row.
    """
    stmt = select(PipelineRun).where(PipelineRun.run_id == payload.run_id)
    run = (await session.execute(stmt)).scalar_one_or_none()

    if run is None:
        run = PipelineRun(
            run_id=payload.run_id,
            workflow=payload.workflow,
            branch=payload.branch,
            pr_number=payload.pr_number,
            sha=payload.sha,
            status=payload.status,
            conclusion=payload.conclusion,
            failed_job=payload.failed_job,
            started_at=payload.started_at,
            finished_at=payload.finished_at,
        )
        session.add(run)
    else:
        run.status = payload.status
        run.conclusion = payload.conclusion
        run.failed_job = payload.failed_job
        run.pr_number = payload.pr_number
        if payload.finished_at is not None:
            run.finished_at = payload.finished_at
    await session.flush()

    heal_job_id: int | None = None
    if payload.status == "completed" and payload.conclusion == "failure":
        fingerprint = f"ci:{payload.branch}:{payload.pr_number or 'none'}"
        in_flight = await _in_flight_ci_job(session, fingerprint)
        if in_flight is not None:
            in_flight.source_pipeline_run_id = run.id
            in_flight.status = HealJobStatus.QUEUED
            in_flight.started_at = None
            await session.flush()
            await notify_heal_job(session, in_flight.id, HealJobType.CI_FAILURE)
            heal_job_id = in_flight.id
        elif payload.pr_number is None or not await ci_fix_circuit_open(
            session, payload.pr_number, max_attempts=settings.max_ci_fix_attempts_per_pr
        ):
            job = await enqueue_heal_job(
                session,
                type=HealJobType.CI_FAILURE,
                fingerprint=fingerprint,
                source_pipeline_run_id=run.id,
                pr_number=payload.pr_number,
            )
            heal_job_id = job.id

    await _audit(
        session,
        action="ci_event",
        actor="sentinel",
        details={
            "run_id": payload.run_id,
            "workflow": payload.workflow,
            "branch": payload.branch,
            "pr_number": payload.pr_number,
            "status": payload.status,
            "conclusion": payload.conclusion,
        },
        heal_job_id=heal_job_id,
    )
    await session.commit()
    return run


async def record_anomaly(
    session: AsyncSession,
    *,
    anomaly_type: str,
    metric_value: float,
    threshold: float,
    window_start: datetime,
    window_end: datetime,
) -> Anomaly | None:
    """Persist an anomaly alert, unless one of the same type fired within the cooldown."""
    cooldown_start = window_end - timedelta(seconds=settings.anomaly_cooldown_seconds)
    stmt = select(Anomaly.id).where(
        Anomaly.type == anomaly_type, Anomaly.created_at >= cooldown_start
    )
    if (await session.execute(stmt)).first() is not None:
        return None

    anomaly = Anomaly(
        type=anomaly_type,
        metric_value=metric_value,
        threshold=threshold,
        window_start=window_start,
        window_end=window_end,
        reported_at=datetime.now(UTC),
    )
    session.add(anomaly)
    await session.flush()

    await _audit(
        session,
        action="anomaly",
        actor="sentinel",
        details={
            "type": anomaly_type,
            "metric_value": metric_value,
            "threshold": threshold,
        },
    )
    await session.commit()
    return anomaly

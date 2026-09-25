"""POST /webhooks/ci: HMAC-verified, replay-protected CI event ingest."""

from __future__ import annotations

import json

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import settings as core_settings
from core.hmac_utils import sign_payload
from core.models import HealJob, HealJobStatus, HealJobType, PipelineRun

WEBHOOK_SECRET = "test-webhook-secret"


@pytest.fixture(autouse=True)
def _configure_webhook_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(core_settings, "healer_webhook_secret", WEBHOOK_SECRET)


def _payload(**overrides: object) -> bytes:
    body = {
        "run_id": 4242,
        "workflow": "ci.yml",
        "branch": "autofix/abc123",
        "sha": "a" * 40,
        "status": "completed",
        "pr_number": 12,
        "conclusion": "failure",
        "failed_job": "pytest",
    }
    body.update(overrides)
    return json.dumps(body).encode()


async def test_validly_signed_webhook_is_accepted_and_stored(
    sentinel_http_client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    body = _payload()
    signature = sign_payload(body, WEBHOOK_SECRET)

    response = await sentinel_http_client.post(
        "/webhooks/ci", content=body, headers={"X-Signature": signature}
    )

    assert response.status_code == 200
    stmt = select(PipelineRun).where(PipelineRun.run_id == 4242)
    run = (await db_session.execute(stmt)).scalar_one()
    assert run.conclusion == "failure"
    assert run.pr_number == 12


async def test_failed_run_enqueues_a_ci_failure_heal_job(
    sentinel_http_client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    body = _payload(run_id=5000)
    signature = sign_payload(body, WEBHOOK_SECRET)

    await sentinel_http_client.post(
        "/webhooks/ci", content=body, headers={"X-Signature": signature}
    )

    # Scoped by fingerprint, not just type=CI_FAILURE - the shared dev
    # database accumulates other tests' heal_jobs of the same type.
    stmt = select(HealJob).where(HealJob.fingerprint == "ci:autofix/abc123:12")
    jobs = (await db_session.execute(stmt)).scalars().all()
    assert len(jobs) == 1
    assert jobs[0].type == HealJobType.CI_FAILURE


async def test_successful_run_does_not_enqueue_a_heal_job(
    sentinel_http_client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    body = _payload(run_id=5001, conclusion="success", failed_job=None)
    signature = sign_payload(body, WEBHOOK_SECRET)

    await sentinel_http_client.post(
        "/webhooks/ci", content=body, headers={"X-Signature": signature}
    )

    stmt = select(HealJob).where(HealJob.fingerprint == "ci:autofix/abc123:12")
    jobs = (await db_session.execute(stmt)).scalars().all()
    assert jobs == []


async def test_unsigned_webhook_is_rejected(sentinel_http_client: httpx.AsyncClient) -> None:
    response = await sentinel_http_client.post("/webhooks/ci", content=_payload())
    assert response.status_code == 401


async def test_wrong_secret_signature_is_rejected(sentinel_http_client: httpx.AsyncClient) -> None:
    body = _payload()
    signature = sign_payload(body, "not-the-real-secret")

    response = await sentinel_http_client.post(
        "/webhooks/ci", content=body, headers={"X-Signature": signature}
    )
    assert response.status_code == 401


async def test_replayed_old_signature_is_rejected(sentinel_http_client: httpx.AsyncClient) -> None:
    body = _payload()
    old_signature = sign_payload(body, WEBHOOK_SECRET, timestamp=1)  # 1970, way outside tolerance

    response = await sentinel_http_client.post(
        "/webhooks/ci", content=body, headers={"X-Signature": old_signature}
    )
    assert response.status_code == 401


async def test_second_failure_on_same_pr_requeues_the_existing_job(
    sentinel_http_client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    """A PR's own fix-commit CI run can fail again while the healer is still
    working on it (see healer.circuit_breaker's module docstring) — that
    must reuse the existing in-flight heal_job, not create a second one."""
    first_body = _payload(run_id=6100, branch="autofix/reuse-test", pr_number=77)
    await sentinel_http_client.post(
        "/webhooks/ci",
        content=first_body,
        headers={"X-Signature": sign_payload(first_body, WEBHOOK_SECRET)},
    )

    second_body = _payload(run_id=6101, branch="autofix/reuse-test", pr_number=77)
    await sentinel_http_client.post(
        "/webhooks/ci",
        content=second_body,
        headers={"X-Signature": sign_payload(second_body, WEBHOOK_SECRET)},
    )

    stmt = select(HealJob).where(HealJob.fingerprint == "ci:autofix/reuse-test:77")
    jobs = (await db_session.execute(stmt)).scalars().all()
    assert len(jobs) == 1

    run_stmt = select(PipelineRun).where(PipelineRun.run_id == 6101)
    run = (await db_session.execute(run_stmt)).scalar_one()
    assert jobs[0].source_pipeline_run_id == run.id
    assert jobs[0].status == HealJobStatus.QUEUED


async def test_circuit_broken_pr_does_not_enqueue_a_new_job(
    sentinel_http_client: httpx.AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(core_settings, "max_ci_fix_attempts_per_pr", 1)

    exhausted = HealJob(
        type=HealJobType.CI_FAILURE,
        status=HealJobStatus.FAILED,
        fingerprint="ci:autofix/exhausted-test:88",
        pr_number=88,
        attempt_count=1,
    )
    db_session.add(exhausted)
    await db_session.flush()

    body = _payload(run_id=6200, branch="autofix/exhausted-test", pr_number=88)
    await sentinel_http_client.post(
        "/webhooks/ci", content=body, headers={"X-Signature": sign_payload(body, WEBHOOK_SECRET)}
    )

    stmt = select(HealJob).where(HealJob.fingerprint == "ci:autofix/exhausted-test:88")
    jobs = (await db_session.execute(stmt)).scalars().all()
    assert len(jobs) == 1  # only the pre-existing, exhausted one


async def test_missing_webhook_secret_configuration_returns_503(
    sentinel_http_client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(core_settings, "healer_webhook_secret", None)
    body = _payload()
    signature = sign_payload(body, WEBHOOK_SECRET)

    response = await sentinel_http_client.post(
        "/webhooks/ci", content=body, headers={"X-Signature": signature}
    )
    assert response.status_code == 503

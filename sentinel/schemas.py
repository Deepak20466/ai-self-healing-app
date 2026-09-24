"""Wire schemas for sentinel-pod's ingest API and CI webhook."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class RequestMetricEvent(BaseModel):
    """One HTTP request's outcome, used only for anomaly detection (not persisted per-row)."""

    path: str
    status_code: int
    duration_ms: float
    occurred_at: datetime


class CIWebhookPayload(BaseModel):
    """Body of `POST /webhooks/ci`, sent by ci.yml / ci-failure.yml (SPEC.md CI/CD section)."""

    run_id: int
    workflow: str
    branch: str
    sha: str
    status: str
    pr_number: int | None = None
    conclusion: str | None = None
    failed_job: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None

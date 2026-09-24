"""sentinel-pod FastAPI app: ingest API, CI webhook, prober + anomaly loops.

Run with: `uvicorn sentinel.app:app --port $SENTINEL_PORT`.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pydantic
from fastapi import Depends, FastAPI, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import settings
from core.db import dispose_engine, get_db
from core.hmac_utils import InvalidSignatureError, verify_signature
from core.logging import configure_logging, get_logger
from sentinel import storage
from sentinel.anomaly import AnomalyDetector, run_anomaly_loop
from sentinel.capture import CapturedError
from sentinel.prober import run_prober_loop
from sentinel.schemas import CIWebhookPayload, RequestMetricEvent

configure_logging(settings.log_level)
logger = get_logger(__name__)

detector = AnomalyDetector()


async def _drain_anomaly_loop(anomaly_detector: AnomalyDetector) -> None:
    async for findings in run_anomaly_loop(anomaly_detector):
        for finding in findings:
            logger.warning("anomaly_detected", type=finding.type, value=finding.metric_value)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    target_app_base_url = f"http://localhost:{settings.app_port}"
    prober_task = asyncio.create_task(run_prober_loop(target_app_base_url))
    anomaly_task = asyncio.create_task(_drain_anomaly_loop(detector))
    logger.info("sentinel_pod_started", target_app_base_url=target_app_base_url)
    try:
        yield
    finally:
        prober_task.cancel()
        anomaly_task.cancel()
        for task in (prober_task, anomaly_task):
            try:
                await task
            except asyncio.CancelledError:
                pass
        await dispose_engine()


app = FastAPI(title="sentinel-pod", lifespan=lifespan)


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok", "pod": "sentinel"}


@app.post("/ingest/error", status_code=status.HTTP_202_ACCEPTED)
async def ingest_error(
    captured: CapturedError, session: AsyncSession = Depends(get_db)
) -> dict[str, object]:
    error = await storage.record_error(session, captured)
    return {"error_id": error.id, "fingerprint": error.fingerprint}


@app.post("/ingest/metric", status_code=status.HTTP_202_ACCEPTED)
async def ingest_metric(event: RequestMetricEvent) -> dict[str, str]:
    detector.record_request(
        status_code=event.status_code,
        duration_ms=event.duration_ms,
        occurred_at=event.occurred_at,
    )
    return {"status": "recorded"}


@app.post("/webhooks/ci")
async def webhooks_ci(
    request: Request, session: AsyncSession = Depends(get_db)
) -> dict[str, object]:
    """HMAC-verified CI event ingest (SPEC.md SECURITY: reject bad/replayed signatures)."""
    if not settings.healer_webhook_secret:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="HEALER_WEBHOOK_SECRET is not configured",
        )

    raw_body = await request.body()
    signature_header = request.headers.get("X-Signature", "")
    try:
        verify_signature(
            raw_body,
            signature_header,
            settings.healer_webhook_secret,
            tolerance_seconds=settings.webhook_replay_tolerance_seconds,
        )
    except InvalidSignatureError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc

    try:
        payload = CIWebhookPayload.model_validate_json(raw_body)
    except pydantic.ValidationError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    run = await storage.record_pipeline_event(session, payload)
    return {"run_id": run.run_id, "status": run.status}

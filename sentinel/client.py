"""Shared HTTP client used by the "drop-in" pieces (middleware, logging handler)
to report to sentinel-pod's ingest API.

Monitoring must never be able to break the app it's monitoring: every method
here swallows transport errors (sentinel-pod down/unreachable) after logging
locally, rather than letting them propagate into the monitored app's request
handling.
"""

from __future__ import annotations

import httpx

from core.config import settings
from core.logging import get_logger
from sentinel.capture import CapturedError
from sentinel.schemas import RequestMetricEvent

logger = get_logger(__name__)


class SentinelClient:
    def __init__(
        self,
        client: httpx.AsyncClient | None = None,
        base_url: str | None = None,
        ingest_token: str | None = None,
    ) -> None:
        self._owns_client = client is None
        token = ingest_token if ingest_token is not None else settings.sentinel_ingest_token
        headers = {"Authorization": f"Bearer {token}"} if token else None
        self._client = client or httpx.AsyncClient(
            base_url=base_url or settings.sentinel_base_url,
            timeout=settings.error_report_timeout_seconds,
            headers=headers,
        )

    async def report_error(self, captured: CapturedError) -> None:
        """Await this one — callers need it durably stored before responding."""
        try:
            await self._client.post("/ingest/error", json=captured.model_dump(mode="json"))
        except httpx.HTTPError as exc:
            logger.warning("sentinel_report_error_failed", error=str(exc))

    async def report_metric(self, event: RequestMetricEvent) -> None:
        """Best-effort, fire-and-forget from the caller's perspective."""
        try:
            await self._client.post("/ingest/metric", json=event.model_dump(mode="json"))
        except httpx.HTTPError as exc:
            logger.debug("sentinel_report_metric_failed", error=str(exc))

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

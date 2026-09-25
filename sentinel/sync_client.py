"""Synchronous counterpart to `sentinel.client.SentinelClient`, for WSGI
frameworks (Flask, Django) whose request handling is itself synchronous --
spinning up an event loop just to await one best-effort POST would be more
complex than a plain blocking `httpx.Client` call, for no real benefit here.

Same contract as the async client: monitoring must never be able to break
the app it monitors, so transport errors are swallowed after logging.
"""

from __future__ import annotations

import httpx

from core.config import settings
from core.logging import get_logger
from sentinel.capture import CapturedError

logger = get_logger(__name__)


class SyncSentinelClient:
    def __init__(
        self,
        client: httpx.Client | None = None,
        base_url: str | None = None,
        ingest_token: str | None = None,
    ) -> None:
        self._owns_client = client is None
        token = ingest_token if ingest_token is not None else settings.sentinel_ingest_token
        headers = {"Authorization": f"Bearer {token}"} if token else None
        self._client = client or httpx.Client(
            base_url=base_url or settings.sentinel_base_url,
            timeout=settings.error_report_timeout_seconds,
            headers=headers,
        )

    def report_error(self, captured: CapturedError) -> None:
        try:
            self._client.post("/ingest/error", json=captured.model_dump(mode="json"))
        except httpx.HTTPError as exc:
            logger.warning("sentinel_report_error_failed", error=str(exc))

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

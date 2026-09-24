"""Drop-in Starlette/FastAPI middleware: the "capture" half of sentinel-pod's
error and metric ingest, run from inside the monitored app's own process.

Error reports are awaited (so tests / demos can rely on "the request
returned" meaning "sentinel has it"); metric reports are fire-and-forget so
they never add latency to the monitored app's normal responses.
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from sentinel.capture import build_captured_error
from sentinel.client import SentinelClient
from sentinel.schemas import RequestMetricEvent


class SentinelMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: object, sentinel_client: SentinelClient | None = None) -> None:
        super().__init__(app)  # type: ignore[arg-type]
        self._client = sentinel_client or SentinelClient()
        self._background_tasks: set[asyncio.Task[None]] = set()

    def _fire_and_forget(self, event: RequestMetricEvent) -> None:
        task = asyncio.create_task(self._client.report_metric(event))
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        start = time.monotonic()
        request_context = {
            "method": request.method,
            "path": request.url.path,
            "query": str(request.url.query),
            "client_host": request.client.host if request.client else None,
            # Raw headers may contain Authorization/Cookie values; storage.py
            # scrubs them by key name before anything is persisted.
            "headers": dict(request.headers),
        }

        try:
            response = await call_next(request)
        except Exception as exc:  # this IS the app's catch-all error boundary
            duration_ms = (time.monotonic() - start) * 1000
            captured = build_captured_error(exc, request_context=request_context)
            await self._client.report_error(captured)
            self._fire_and_forget(
                RequestMetricEvent(
                    path=request.url.path,
                    status_code=500,
                    duration_ms=duration_ms,
                    occurred_at=captured.occurred_at,
                )
            )
            return JSONResponse({"detail": "Internal Server Error"}, status_code=500)

        duration_ms = (time.monotonic() - start) * 1000
        self._fire_and_forget(
            RequestMetricEvent(
                path=request.url.path,
                status_code=response.status_code,
                duration_ms=duration_ms,
                occurred_at=datetime.now(UTC),
            )
        )
        return response

"""Drop-in Django middleware -- the sync counterpart to
`sentinel.middleware.SentinelMiddleware` (Starlette/FastAPI).

Implements `process_exception`, Django's dedicated hook for unhandled view
exceptions (called before Django converts it into its own 500 response),
the same principle as Flask's `got_request_exception` signal in
`sentinel/flask_middleware.py`.

Usage: add `"sentinel.django_middleware.SentinelDjangoMiddleware"` to
`MIDDLEWARE` in the monitored app's Django settings. `django` is an
optional dependency of this package (only needed by apps using this
integration).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from sentinel.capture import build_captured_error
from sentinel.sync_client import SyncSentinelClient

_IN_APP_MARKER: str | None = None


class SentinelDjangoMiddleware:
    def __init__(self, get_response: Callable[[Any], Any]) -> None:
        self.get_response = get_response
        self._client = SyncSentinelClient()

    def __call__(self, request: Any) -> Any:
        return self.get_response(request)

    def process_exception(self, request: Any, exception: BaseException) -> None:
        request_context = {
            "method": request.method,
            "path": request.path,
            "query": request.META.get("QUERY_STRING", ""),
        }
        markers = (_IN_APP_MARKER,) if _IN_APP_MARKER else None
        captured = build_captured_error(
            exception, request_context=request_context, in_app_markers=markers
        )
        self._client.report_error(captured)
        return None  # let Django continue its own normal exception handling

"""Drop-in Flask integration -- the sync counterpart to
`sentinel.middleware.SentinelMiddleware` (Starlette/FastAPI).

Flask always fires the `got_request_exception` signal whenever a view
raises, even when it goes on to convert that into its own 500 response --
that's the right hook, not a raw WSGI try/except wrapper, since it fires
regardless of Flask's DEBUG/TESTING/PROPAGATE_EXCEPTIONS settings (a raw
WSGI wrapper would miss exceptions Flask has already handled internally).

Usage: `init_sentinel_flask(app)` once, after creating the Flask app.
`flask` is an optional dependency of this package (only needed if a
monitored app actually uses this integration).
"""

from __future__ import annotations

from typing import Any

from sentinel.capture import build_captured_error
from sentinel.sync_client import SyncSentinelClient


def init_sentinel_flask(
    app: Any,
    sentinel_client: SyncSentinelClient | None = None,
    in_app_marker: str | None = None,
) -> SyncSentinelClient:
    """Wire sentinel error capture into a Flask app. Returns the client used
    (so the caller can `.close()` it on shutdown if desired)."""
    from flask import got_request_exception, request

    client = sentinel_client or SyncSentinelClient()
    markers = (in_app_marker,) if in_app_marker else None

    def _on_exception(sender: Any, exception: BaseException, **extra: Any) -> None:
        request_context = {
            "method": request.method,
            "path": request.path,
            "query": request.query_string.decode("utf-8", errors="replace"),
        }
        captured = build_captured_error(
            exception, request_context=request_context, in_app_markers=markers
        )
        client.report_error(captured)

    # weak=False: `_on_exception` is a local closure with no other strong
    # reference anywhere -- blinker's default weak-reference connection
    # would let it be garbage-collected right after this function returns,
    # silently disconnecting the signal.
    got_request_exception.connect(_on_exception, app, weak=False)
    return client

"""target_app (app-pod) FastAPI entrypoint.

Run with: `uvicorn apps.target_app.main:app --port $APP_PORT`.

`create_app` is a factory (rather than a bare module-level `app`) so tests
can inject a `SentinelClient` wired to sentinel-pod's in-process ASGI app
via `httpx.ASGITransport`, instead of a real client hitting a real socket.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from apps.target_app.routes import router
from core.config import settings
from core.db import dispose_engine
from core.logging import configure_logging
from sentinel.client import SentinelClient
from sentinel.logging_handler import SentinelLogHandler
from sentinel.middleware import SentinelMiddleware

configure_logging(settings.log_level)


def create_app(sentinel_client: SentinelClient | None = None) -> FastAPI:
    # Re-attach cleanly on every call (tests call create_app() repeatedly) so
    # a stale handler from a previous app instance never lingers on the
    # shared, name-keyed logger.
    target_logger = logging.getLogger("apps.target_app")
    for existing in list(target_logger.handlers):
        if isinstance(existing, SentinelLogHandler):
            target_logger.removeHandler(existing)
    target_logger.addHandler(SentinelLogHandler(client=sentinel_client))

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        yield
        await dispose_engine()

    app = FastAPI(title="target_app", lifespan=lifespan)
    app.add_middleware(SentinelMiddleware, sentinel_client=sentinel_client)
    app.include_router(router)
    return app


app = create_app()

"""Drop-in `logging.Handler`: captures errors that are logged (with
`logger.exception(...)`) but swallowed rather than left to propagate — the
other half of SPEC.md's "a drop-in FastAPI middleware and a logging.Handler
in the target app capture" requirement. `SentinelMiddleware` only ever sees
exceptions that escape a route handler; this handler catches the ones that
don't.
"""

from __future__ import annotations

import asyncio
import logging

from sentinel.capture import build_captured_error
from sentinel.client import SentinelClient


class SentinelLogHandler(logging.Handler):
    def __init__(self, client: SentinelClient | None = None, level: int = logging.ERROR) -> None:
        super().__init__(level)
        self._client = client or SentinelClient()
        self._background_tasks: set[asyncio.Task[None]] = set()

    def emit(self, record: logging.LogRecord) -> None:
        if not record.exc_info or record.exc_info[1] is None:
            return

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # No running event loop (e.g. emitted from sync startup code) -
            # skip remote reporting rather than crash the caller.
            return

        task = loop.create_task(self._report(record))
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    async def _report(self, record: logging.LogRecord) -> None:
        assert record.exc_info is not None and record.exc_info[1] is not None
        exc = record.exc_info[1]
        captured = build_captured_error(
            exc,
            request_context={"logger": record.name, "log_message": record.getMessage()},
        )
        await self._client.report_error(captured)

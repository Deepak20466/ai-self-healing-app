"""SentinelLogHandler: captures logger.exception(...) calls that never
propagate as an unhandled exception (the other half of SPEC.md's "a drop-in
FastAPI middleware and a logging.Handler in the target app capture").
"""

from __future__ import annotations

import asyncio
import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.models import Error
from sentinel.logging_handler import SentinelLogHandler


async def test_logged_exception_is_reported_to_sentinel(
    sentinel_client_for_target_app, db_session: AsyncSession
) -> None:
    test_logger = logging.getLogger("test_sentinel_logging_handler")
    test_logger.setLevel(logging.ERROR)
    handler = SentinelLogHandler(client=sentinel_client_for_target_app)
    test_logger.addHandler(handler)
    try:
        try:
            raise RuntimeError("swallowed but logged")
        except RuntimeError:
            test_logger.exception("something went wrong but was handled")

        # emit() only schedules a task; wait for the exact one it created
        # instead of a fixed sleep (avoids a race with fixture teardown).
        await asyncio.gather(*handler._background_tasks)
    finally:
        test_logger.removeHandler(handler)

    stmt = select(Error).where(Error.exception_type == "RuntimeError")
    error = (await db_session.execute(stmt)).scalar_one()
    assert error.message == "swallowed but logged"


async def test_log_records_without_exc_info_are_ignored(
    sentinel_client_for_target_app, db_session: AsyncSession
) -> None:
    test_logger = logging.getLogger("test_sentinel_logging_handler_no_exc")
    test_logger.setLevel(logging.ERROR)
    handler = SentinelLogHandler(client=sentinel_client_for_target_app)
    test_logger.addHandler(handler)
    try:
        test_logger.error("just an error message, no exception")
        assert handler._background_tasks == set()  # no exc_info -> emit() returns early
    finally:
        test_logger.removeHandler(handler)

    stmt = select(Error).where(Error.message == "just an error message, no exception")
    error = (await db_session.execute(stmt)).scalar_one_or_none()
    assert error is None


def test_emit_without_a_running_event_loop_does_not_raise() -> None:
    """emit() is called synchronously; with no running loop it must degrade safely."""
    handler = SentinelLogHandler()
    record = logging.LogRecord(
        name="test",
        level=logging.ERROR,
        pathname=__file__,
        lineno=1,
        msg="boom",
        args=(),
        exc_info=None,
    )
    handler.emit(record)  # no exc_info -> returns early, no loop needed either way

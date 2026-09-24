"""End-to-end: hit target_app, verify sentinel captured it with the right file/line.

This is SPEC.md Phase 2's explicit acceptance check: "/trigger/zero stores
the error with the correct file and line." Everything runs in-process
(target_app -> SentinelMiddleware -> sentinel's ASGI app -> the same
per-test DB transaction), so no real server processes or sockets are needed.

Queries are scoped by the exact fingerprint each bug produces, not just by
`exception_type` - the shared dev database accumulates other tests' rows
(e.g. mcp_server tool tests exercising `get_error` with an arbitrary
`exception_type` like "KeyError"), so filtering by type alone is fragile.
"""

from __future__ import annotations

import inspect

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.target_app import bugs
from core.models import Error, ErrorOccurrence
from sentinel.fingerprint import fingerprint_error


async def test_trigger_zero_stores_error_with_correct_file_and_line(
    target_app_client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    response = await target_app_client.get("/trigger/zero")
    assert response.status_code == 500

    fingerprint = fingerprint_error(
        "ZeroDivisionError", "apps/target_app/bugs.py", "average_rating"
    )
    stmt = select(Error).where(Error.fingerprint == fingerprint)
    error = (await db_session.execute(stmt)).scalar_one()

    assert error.file_path == "apps/target_app/bugs.py"
    assert error.function_name == "average_rating"
    assert "division" in error.message.lower()
    assert "ZeroDivisionError" in error.traceback

    # The reported line must actually fall inside average_rating()'s source.
    start_line, source_lines = _function_line_range(bugs.average_rating)
    assert start_line <= error.line_number < start_line + len(source_lines)

    assert error.occurrence_count == 1
    assert error.status.value == "open"


async def test_repeated_trigger_increments_occurrence_count(
    target_app_client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    await target_app_client.get("/trigger/key")
    await target_app_client.get("/trigger/key")

    fingerprint = fingerprint_error("KeyError", "apps/target_app/bugs.py", "order_status_label")
    stmt = select(Error).where(Error.fingerprint == fingerprint)
    error = (await db_session.execute(stmt)).scalar_one()
    assert error.occurrence_count == 2

    occurrences_stmt = select(ErrorOccurrence).where(ErrorOccurrence.error_id == error.id)
    occurrences = (await db_session.execute(occurrences_stmt)).scalars().all()
    assert len(occurrences) == 2


async def test_secret_looking_data_is_scrubbed_before_storage(
    target_app_client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    response = await target_app_client.get(
        "/trigger/none_lookup", headers={"Authorization": "Bearer sk-ant-not-a-real-secret-value"}
    )
    assert response.status_code == 500

    fingerprint = fingerprint_error("AttributeError", "apps/target_app/bugs.py", "item_label")
    stmt = select(Error).where(Error.fingerprint == fingerprint)
    error = (await db_session.execute(stmt)).scalar_one()
    assert "sk-ant-not-a-real-secret-value" not in str(error.request_context)


def _function_line_range(func: object) -> tuple[int, list[str]]:
    source_lines, start_line = inspect.getsourcelines(func)  # type: ignore[arg-type]
    return start_line, source_lines

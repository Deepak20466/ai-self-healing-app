"""mcp_server.tools.errors: get_error, list_open_errors, get_contract_violation.

These tools open their own DB session via `core.db.session_scope()` (MCP
tools have no request-scoped dependency injection to override), so test
data here is created the same way — a real, committed row — rather than via
the rollback-wrapped `db_session` fixture, which would be invisible to the
tool's independent transaction. Rows use a random suffix per test run and
are harmless to leave behind (same tradeoff already accepted for
sentinel/anomaly.py and mcp_server/audit.py).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from core.db import session_scope
from core.models import ContractViolation, Error, OpenResolvedStatus
from mcp_server.tools._exceptions import ToolError
from mcp_server.tools.errors import get_contract_violation, get_error, list_open_errors


async def _make_error(**overrides: object) -> Error:
    suffix = uuid.uuid4().hex[:8]
    defaults: dict[str, object] = {
        "fingerprint": f"fp-{suffix}",
        "exception_type": "ValueError",
        "message": "boom",
        "traceback": "Traceback...\nValueError: boom",
        "file_path": "apps/target_app/bugs.py",
        "line_number": 10,
        "function_name": "some_func",
        "status": OpenResolvedStatus.OPEN,
        "occurrence_count": 1,
        "first_seen_at": datetime.now(UTC),
        "last_seen_at": datetime.now(UTC),
    }
    defaults.update(overrides)
    async with session_scope() as session:
        error = Error(**defaults)  # type: ignore[arg-type]
        session.add(error)
        await session.flush()
        await session.refresh(error)
        session.expunge(error)
    return error


async def _make_violation(**overrides: object) -> ContractViolation:
    suffix = uuid.uuid4().hex[:8]
    defaults: dict[str, object] = {
        "fingerprint": f"cv-{suffix}",
        "endpoint": "/items/top",
        "expected": "{'a': 1}",
        "actual": "{'a': 2}",
        "file_path": "apps/target_app/bugs.py",
        "line_number": 20,
        "status": OpenResolvedStatus.OPEN,
        "occurrence_count": 1,
        "first_seen_at": datetime.now(UTC),
        "last_seen_at": datetime.now(UTC),
    }
    defaults.update(overrides)
    async with session_scope() as session:
        violation = ContractViolation(**defaults)  # type: ignore[arg-type]
        session.add(violation)
        await session.flush()
        await session.refresh(violation)
        session.expunge(violation)
    return violation


async def test_get_error_returns_full_detail() -> None:
    # Deliberately not "KeyError"/"ZeroDivisionError"/etc - those are the
    # real seeded bugs' exception types, and other tests query the shared
    # dev DB by exact fingerprint for them; an arbitrary distinct type here
    # avoids adding noise to that fingerprint space.
    error = await _make_error(exception_type="McpToolsErrorsTestType")
    result = await get_error(error.id)
    assert result["exception_type"] == "McpToolsErrorsTestType"
    assert result["file_path"] == "apps/target_app/bugs.py"


async def test_get_error_missing_raises_tool_error() -> None:
    with pytest.raises(ToolError):
        await get_error(2**62)


async def test_list_open_errors_excludes_resolved() -> None:
    open_error = await _make_error(exception_type="OpenOne")
    await _make_error(exception_type="ResolvedOne", status=OpenResolvedStatus.RESOLVED)

    results = await list_open_errors(limit=500)
    ids = {r["id"] for r in results}
    assert open_error.id in ids
    assert all(r["status"] == "open" for r in results)


async def test_get_contract_violation_returns_detail() -> None:
    violation = await _make_violation(endpoint="/orders/102/delivery-estimate")
    result = await get_contract_violation(violation.id)
    assert result["endpoint"] == "/orders/102/delivery-estimate"


async def test_get_contract_violation_missing_raises_tool_error() -> None:
    with pytest.raises(ToolError):
        await get_contract_violation(2**62)

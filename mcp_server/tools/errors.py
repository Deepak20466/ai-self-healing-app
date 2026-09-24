"""Error tools: get_error, list_open_errors, get_contract_violation."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from core.db import session_scope
from core.models import ContractViolation, Error, OpenResolvedStatus
from mcp_server.audit import audited_tool
from mcp_server.instance import mcp
from mcp_server.tools._exceptions import ToolError


def _error_to_dict(error: Error) -> dict[str, Any]:
    return {
        "id": error.id,
        "fingerprint": error.fingerprint,
        "exception_type": error.exception_type,
        "message": error.message,
        "traceback": error.traceback,
        "file_path": error.file_path,
        "line_number": error.line_number,
        "function_name": error.function_name,
        "status": error.status.value,
        "occurrence_count": error.occurrence_count,
        "first_seen_at": error.first_seen_at.isoformat(),
        "last_seen_at": error.last_seen_at.isoformat(),
    }


def _violation_to_dict(violation: ContractViolation) -> dict[str, Any]:
    return {
        "id": violation.id,
        "fingerprint": violation.fingerprint,
        "endpoint": violation.endpoint,
        "expected": violation.expected,
        "actual": violation.actual,
        "file_path": violation.file_path,
        "line_number": violation.line_number,
        "status": violation.status.value,
        "occurrence_count": violation.occurrence_count,
        "first_seen_at": violation.first_seen_at.isoformat(),
        "last_seen_at": violation.last_seen_at.isoformat(),
    }


@audited_tool(mcp, "get_error")
async def get_error(error_id: int) -> dict[str, Any]:
    """Fetch a single captured runtime error by id, including its full traceback."""
    async with session_scope() as session:
        error = await session.get(Error, error_id)
        if error is None:
            raise ToolError(f"No error with id {error_id}")
        return _error_to_dict(error)


@audited_tool(mcp, "list_open_errors")
async def list_open_errors(limit: int = 50) -> list[dict[str, Any]]:
    """List errors with status=open, most recently seen first."""
    async with session_scope() as session:
        stmt = (
            select(Error)
            .where(Error.status == OpenResolvedStatus.OPEN)
            .order_by(Error.last_seen_at.desc())
            .limit(limit)
        )
        errors = (await session.execute(stmt)).scalars().all()
        return [_error_to_dict(e) for e in errors]


@audited_tool(mcp, "get_contract_violation")
async def get_contract_violation(violation_id: int) -> dict[str, Any]:
    """Fetch a single contract (silent-bug) violation by id."""
    async with session_scope() as session:
        violation = await session.get(ContractViolation, violation_id)
        if violation is None:
            raise ToolError(f"No contract violation with id {violation_id}")
        return _violation_to_dict(violation)

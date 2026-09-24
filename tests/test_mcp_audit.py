"""mcp_server.audit: every tool call gets logged, scrubbed, and truncated."""

from __future__ import annotations

from mcp_server.audit import log_tool_call


async def test_successful_call_is_logged_with_scrubbed_and_truncated_args(db_session) -> None:
    from sqlalchemy import select

    from core.models import AuditLog

    await log_tool_call(
        "read_file",
        {"path": "apps/target_app/bugs.py", "secret_token": "sk-ant-abcdefghijklmnop"},
        success=True,
        duration_ms=12.3,
    )

    stmt = select(AuditLog).where(AuditLog.action == "tool_call")
    row = (await db_session.execute(stmt)).scalars().first()
    assert row is not None
    assert row.actor == "mcp"
    assert row.details["tool"] == "read_file"
    assert row.details["success"] is True
    assert row.details["args"]["secret_token"] == "[REDACTED]"
    assert row.details["args"]["path"] == "apps/target_app/bugs.py"


async def test_failed_call_logs_the_error_message(db_session) -> None:
    from sqlalchemy import select

    from core.models import AuditLog

    await log_tool_call(
        "get_error",
        {"error_id": 999999},
        success=False,
        duration_ms=5.0,
        error="No error with id 999999",
    )

    stmt = select(AuditLog).where(AuditLog.action == "tool_call")
    rows = (await db_session.execute(stmt)).scalars().all()
    matching = [r for r in rows if r.details.get("tool") == "get_error"]
    assert matching
    assert matching[0].details["success"] is False
    assert "999999" in matching[0].details["error"]


async def test_long_argument_values_are_truncated(db_session) -> None:
    from sqlalchemy import select

    from core.models import AuditLog

    long_diff = "x" * 5000
    await log_tool_call("propose_patch", {"unified_diff": long_diff}, success=True, duration_ms=1.0)

    stmt = select(AuditLog).where(AuditLog.action == "tool_call")
    rows = (await db_session.execute(stmt)).scalars().all()
    matching = [r for r in rows if r.details.get("tool") == "propose_patch"]
    assert matching
    logged_value = matching[0].details["args"]["unified_diff"]
    assert len(logged_value) < 1000
    assert "chars omitted" in logged_value

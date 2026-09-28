"""mcp_server.audit: every tool call gets logged, scrubbed, and truncated.

Every query here orders by `AuditLog.id.desc()` and takes the newest matching
row: from Phase 4 on, `healer`'s end-to-end tests make real, committed
`propose_patch`/`read_file`/etc. tool calls of their own (via the real
MCP server), which leave earlier "tool_call" rows with the same tool name
(and sometimes the same args) in the shared `selfheal_test` DB. The row this
test just logged is always the newest one, so ordering avoids picking up one
of those instead.
"""

from __future__ import annotations

from typing import Any

from mcp_server.audit import audited_tool, log_tool_call


async def test_successful_call_is_logged_with_scrubbed_and_truncated_args(db_session) -> None:
    from sqlalchemy import select

    from core.models import AuditLog

    await log_tool_call(
        "read_file",
        {"path": "apps/target_app/bugs.py", "secret_token": "sk-ant-abcdefghijklmnop"},
        success=True,
        duration_ms=12.3,
    )

    stmt = select(AuditLog).where(AuditLog.action == "tool_call").order_by(AuditLog.id.desc())
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

    stmt = select(AuditLog).where(AuditLog.action == "tool_call").order_by(AuditLog.id.desc())
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

    stmt = select(AuditLog).where(AuditLog.action == "tool_call").order_by(AuditLog.id.desc())
    rows = (await db_session.execute(stmt)).scalars().all()
    matching = [r for r in rows if r.details.get("tool") == "propose_patch"]
    assert matching
    logged_value = matching[0].details["args"]["unified_diff"]
    assert len(logged_value) < 1000
    assert "chars omitted" in logged_value


class _FakeMcp:
    """Minimal stand-in for MCPServer.tool(): registers nothing, just returns
    the function unwrapped -- audited_tool's own wrapper is what's under test."""

    def tool(self, name: str | None = None, **kwargs: Any):  # noqa: ANN201
        def decorator(func):  # noqa: ANN001, ANN202
            return func

        return decorator


async def test_audited_tool_scrubs_the_returned_result_not_just_logged_args(
    db_session,
) -> None:
    """Privacy guard (terminal-only v1.0 Step 5): a tool's RETURN VALUE is
    what actually reaches whichever AI backend made the call (via
    MCPToolClient.call_tool, or directly for a CLI backend talking to
    mcp-pod) -- log_tool_call only ever scrubbed the logged args, never the
    result handed back to the caller. This is the regression test for that
    gap."""

    @audited_tool(_FakeMcp(), "fake_tool_with_a_leaky_result")
    async def fake_tool(error_id: int) -> dict[str, Any]:
        return {
            "error_id": error_id,
            "message": "connection failed: postgresql://svc:hunter2@dbhost:5432/app",
            "github_token": "ghp_" + "a" * 36,
        }

    result = await fake_tool(error_id=1)
    assert "hunter2" not in result["message"]
    assert "svc" not in result["message"]
    assert result["github_token"] == "[REDACTED]"

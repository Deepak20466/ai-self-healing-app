"""Log every MCP tool call to `audit_log` (SPEC.md mcp-pod section).

`audited_tool` is a drop-in replacement for `@mcp.tool()` that wraps
registration with before/after audit logging — every tool in `tools/*.py`
uses it instead of `@mcp.tool()` directly, so logging can never be forgotten
on a new tool.
"""

from __future__ import annotations

import functools
import time
from collections.abc import Awaitable, Callable
from typing import Any, ParamSpec, TypeVar

from core.db import session_scope
from core.models import AuditLog
from sentinel.scrubber import scrub_dict

P = ParamSpec("P")
R = TypeVar("R")

_MAX_LOGGED_VALUE_LENGTH = 500


def _truncate_values(data: dict[str, Any]) -> dict[str, Any]:
    truncated: dict[str, Any] = {}
    for key, value in data.items():
        if isinstance(value, str) and len(value) > _MAX_LOGGED_VALUE_LENGTH:
            omitted = len(value) - _MAX_LOGGED_VALUE_LENGTH
            truncated[key] = f"{value[:_MAX_LOGGED_VALUE_LENGTH]}...<{omitted} chars omitted>"
        else:
            truncated[key] = value
    return truncated


async def log_tool_call(
    tool_name: str,
    kwargs: dict[str, Any],
    *,
    success: bool,
    duration_ms: float,
    error: str | None = None,
) -> None:
    scrubbed_args = _truncate_values(scrub_dict(kwargs))
    details: dict[str, Any] = {
        "tool": tool_name,
        "args": scrubbed_args,
        "success": success,
        "duration_ms": round(duration_ms, 2),
    }
    if error is not None:
        details["error"] = error[:2000]

    async with session_scope() as session:
        session.add(AuditLog(action="tool_call", actor="mcp", details=details))


def audited_tool(
    mcp_instance: Any, name: str | None = None, **tool_kwargs: Any
) -> Callable[[Callable[P, Awaitable[R]]], Callable[P, Awaitable[R]]]:
    """`@audited_tool(mcp, "read_file")` registers + audit-logs every call."""

    def decorator(func: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R]]:
        tool_name = name or func.__name__

        @functools.wraps(func)
        async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            start = time.monotonic()
            try:
                result = await func(*args, **kwargs)
            except Exception as exc:
                await log_tool_call(
                    tool_name,
                    dict(kwargs),
                    success=False,
                    duration_ms=(time.monotonic() - start) * 1000,
                    error=str(exc),
                )
                raise
            else:
                await log_tool_call(
                    tool_name,
                    dict(kwargs),
                    success=True,
                    duration_ms=(time.monotonic() - start) * 1000,
                )
                return result

        registered: Callable[P, Awaitable[R]] = mcp_instance.tool(name=tool_name, **tool_kwargs)(
            wrapper
        )
        return registered

    return decorator

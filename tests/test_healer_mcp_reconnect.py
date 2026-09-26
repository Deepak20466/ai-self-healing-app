"""ReconnectingMCPToolClient recovers after the server connection dies."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

import pytest

from healer.mcp_client import MCPToolClient, MCPToolError, ReconnectingMCPToolClient


class _FlakyClient(MCPToolClient):
    def __init__(self, fail: Exception | None) -> None:
        super().__init__()
        self._fail = fail

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        if self._fail is not None:
            raise self._fail
        return {"ok": name}


def _factory(connects: list[int], failing_first: int):
    @asynccontextmanager
    async def connect():
        connects.append(1)
        yield _FlakyClient(ConnectionError("dead") if len(connects) <= failing_first else None)

    return connect


async def test_reconnects_after_connection_error() -> None:
    connects: list[int] = []
    client = ReconnectingMCPToolClient(_factory(connects, 1), base_delay_s=0, ready_timeout_s=0.5)
    await client.start()
    try:
        assert await client.call_tool("get_metrics", {}) == {"ok": "get_metrics"}
        assert len(connects) == 2  # first connection was dead, second worked
    finally:
        await client.aclose()


async def test_tool_errors_are_not_retried() -> None:
    connects: list[int] = []

    @asynccontextmanager
    async def connect():
        connects.append(1)
        yield _FlakyClient(MCPToolError("bad arg"))

    client = ReconnectingMCPToolClient(connect, base_delay_s=0, ready_timeout_s=0.5)
    await client.start()
    try:
        with pytest.raises(MCPToolError):
            await client.call_tool("x", {})
        assert len(connects) == 1
    finally:
        await client.aclose()


async def test_gives_up_after_attempts() -> None:
    client = ReconnectingMCPToolClient(
        _factory([], 99), attempts=3, base_delay_s=0, ready_timeout_s=0.5
    )
    await client.start()
    try:
        with pytest.raises(ConnectionError):
            await client.call_tool("x", {})
    finally:
        await client.aclose()

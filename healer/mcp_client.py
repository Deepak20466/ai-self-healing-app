"""MCP client wrapper: real streamable-HTTP in production, an in-memory
transport wired directly to mcp_server's `MCPServer` instance in tests.

SPEC.md says the healer is "connected via the MCP client over streamable
HTTP" — `connect_http` is that, used by `healer/worker.py`. Tests use
`connect_in_memory`, which runs the actual `mcp_server.server.mcp` instance
in-process via the `mcp` SDK's own `InMemoryTransport`: real MCP protocol
messages, real tool registration, real sandboxing — zero sockets. This is the
same in-process-over-a-real-transport approach `tests/conftest.py` already
uses for target_app/sentinel via `httpx.ASGITransport`.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from mcp.client._memory import InMemoryTransport
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.server.mcpserver import MCPServer


class MCPToolError(Exception):
    """Raised when an MCP tool call returns `is_error=True` (a `ToolError` server-side)."""


def _content_text(result: Any) -> str:
    return "".join(getattr(block, "text", "") for block in result.content)


class MCPToolClient:
    """A connected `ClientSession` plus a JSON-friendly `call_tool`/list API."""

    def __init__(self, session: ClientSession) -> None:
        self._session = session

    async def list_tool_schemas(self) -> list[dict[str, Any]]:
        """Anthropic-`tools`-shaped schemas: `{name, description, input_schema}`."""
        result = await self._session.list_tools()
        return [
            {
                "name": tool.name,
                "description": tool.description or "",
                "input_schema": tool.input_schema,
            }
            for tool in result.tools
        ]

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        """Call `name` and return its structured result, raising `MCPToolError` on failure."""
        result = await self._session.call_tool(name, arguments)
        if result.is_error:
            text = _content_text(result)
            raise MCPToolError(text or f"Tool {name!r} failed with no error detail")
        if result.structured_content is not None:
            return result.structured_content
        return _content_text(result)


@asynccontextmanager
async def connect_in_memory(server: MCPServer[Any]) -> AsyncIterator[MCPToolClient]:
    """Connect to `server` in-process, no sockets. Used by tests only — production
    always goes through `connect_http`, per SPEC.md's streamable-HTTP requirement."""
    async with InMemoryTransport(server) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            yield MCPToolClient(session)


@asynccontextmanager
async def connect_http(url: str) -> AsyncIterator[MCPToolClient]:
    """Connect to the real mcp-pod over streamable HTTP. Used by `healer/worker.py`."""
    async with streamable_http_client(url) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            yield MCPToolClient(session)

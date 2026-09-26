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

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Any, cast

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

    def __init__(self, session: ClientSession | None = None) -> None:
        self._session = cast("ClientSession", session)  # subclasses override every call

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


class ReconnectingMCPToolClient(MCPToolClient):
    """`MCPToolClient` that transparently re-establishes its connection.

    healer-pod holds one long-lived connection to mcp-pod; if mcp-pod restarts
    that connection is dead for good, so every chat/dashboard call would fail
    until healer itself restarted. A dedicated background task owns the
    connection (anyio cancel scopes must be entered and exited in the *same*
    task, so request handlers can't be the ones to tear it down): it connects,
    waits until a caller flags the connection dead, then reconnects with
    exponential backoff. Callers retry on any transport-level failure, but
    never on `MCPToolError` (a healthy server reporting a tool error).
    """

    def __init__(
        self,
        connect: Callable[[], AbstractAsyncContextManager[MCPToolClient]],
        *,
        attempts: int = 6,
        base_delay_s: float = 0.5,
        max_delay_s: float = 5.0,
        call_timeout_s: float = 30.0,
        ready_timeout_s: float = 10.0,
    ) -> None:
        self._connect = connect
        self._attempts = attempts
        self._base_delay_s = base_delay_s
        self._max_delay_s = max_delay_s
        self._call_timeout_s = call_timeout_s
        self._ready_timeout_s = ready_timeout_s
        self._inner: MCPToolClient | None = None
        self._ready = asyncio.Event()
        self._dead = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        """Start the connection task and wait (briefly) for the first connect.

        Never raises: if mcp-pod isn't up yet, calls retry until it is.
        """
        self._task = asyncio.create_task(self._run())
        try:
            await asyncio.wait_for(self._ready.wait(), self._ready_timeout_s)
        except TimeoutError:
            pass  # noqa: S110

    async def aclose(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except BaseException:  # noqa: BLE001, S110 - shutting down; nothing useful to surface
                pass  # noqa: S110
            self._task = None

    async def _run(self) -> None:
        failures = 0
        while True:
            try:
                async with self._connect() as client:
                    self._inner = client
                    self._ready.set()
                    failures = 0
                    await self._dead.wait()
            except asyncio.CancelledError:
                raise
            except BaseException:  # noqa: BLE001, S110 - task-group errors etc.; reconnect regardless
                pass  # noqa: S110
            finally:
                self._inner = None
                self._ready.clear()
                self._dead.clear()
            await asyncio.sleep(min(self._base_delay_s * (2**failures), self._max_delay_s))
            failures += 1

    async def _with_retry(self, op: Callable[[MCPToolClient], Awaitable[Any]]) -> Any:
        last: BaseException | None = None
        for attempt in range(self._attempts):
            try:
                await asyncio.wait_for(self._ready.wait(), self._ready_timeout_s)
                inner = self._inner
                if inner is None:
                    continue
                try:
                    return await asyncio.wait_for(op(inner), self._call_timeout_s)
                except MCPToolError:
                    raise
                except Exception:
                    if self._inner is inner:
                        self._dead.set()
                    raise
            except MCPToolError:
                raise
            except Exception as exc:  # noqa: BLE001 - any transport failure means reconnect
                last = exc
                await asyncio.sleep(min(self._base_delay_s * (2**attempt), self._max_delay_s))
        raise ConnectionError(f"mcp-pod unreachable after {self._attempts} attempts: {last!r}")

    async def list_tool_schemas(self) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = await self._with_retry(lambda c: c.list_tool_schemas())
        return result

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        return await self._with_retry(lambda c: c.call_tool(name, arguments))

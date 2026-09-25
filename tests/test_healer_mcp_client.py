"""healer.mcp_client: a real MCP client<->server round trip, in-memory.

Uses the SDK's own `InMemoryTransport` against the actual, fully-registered
`mcp_server.server.mcp` instance — real MCP protocol messages, real tool
execution and sandboxing, zero sockets. This is genuinely new coverage versus
`tests/test_mcp_server_registration.py` (which calls `mcp.list_tools()`
in-process with no transport at all): it's the first test that exercises the
wire protocol `healer/worker.py` depends on, just without a real TCP socket.
"""

from __future__ import annotations

import pytest

from healer.mcp_client import MCPToolError, connect_in_memory
from mcp_server.server import mcp


async def test_list_tool_schemas_includes_known_tools_with_object_schemas() -> None:
    async with connect_in_memory(mcp) as client:
        schemas = await client.list_tool_schemas()

    by_name = {schema["name"]: schema for schema in schemas}
    for name in ("read_file", "run_tests", "propose_patch", "get_error"):
        assert name in by_name
        assert by_name[name]["input_schema"]["type"] == "object"


async def test_call_tool_returns_structured_content_for_a_dict_result() -> None:
    async with connect_in_memory(mcp) as client:
        result = await client.call_tool("read_file", {"path": "pyproject.toml"})

    assert isinstance(result, dict)
    assert "requires-python" in result["content"]


async def test_call_tool_raises_on_a_tool_error() -> None:
    async with connect_in_memory(mcp) as client:
        with pytest.raises(MCPToolError, match="No error with id"):
            await client.call_tool("get_error", {"error_id": 999_999_999})

"""Re-export the MCP SDK's tool/resource exceptions from one place.

Raise `ToolError` for anticipated failures (not found, invalid input, patch
rejected) — the client gets a clean message and the server doesn't log a
traceback. Any other exception is treated as a crash (see
`mcp.server.mcpserver.exceptions` docstrings for the exact wire behavior).
"""

from __future__ import annotations

from mcp.server.mcpserver.exceptions import (
    ResourceError,
    ResourceNotFoundError,
    ToolError,
)

__all__ = ["ResourceError", "ResourceNotFoundError", "ToolError"]

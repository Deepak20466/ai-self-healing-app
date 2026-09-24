"""mcp-pod entrypoints.

- stdio (for `.mcp.json` / Claude Code in VS Code): `python -m mcp_server.server`
- streamable HTTP (the real mcp-pod process; the healer connects to this):
  `python -m mcp_server.http_main`
"""

from __future__ import annotations

from core.config import settings
from core.logging import configure_logging
from mcp_server import resources  # noqa: F401 - registers resources by import
from mcp_server.instance import mcp
from mcp_server.tools import cicd, code, deploy, errors  # noqa: F401 - registers tools by import

configure_logging(settings.log_level)

__all__ = ["mcp", "run_stdio", "run_http"]


def run_stdio() -> None:
    mcp.run(transport="stdio")


def run_http() -> None:
    mcp.run(transport="streamable-http", host="127.0.0.1", port=settings.mcp_port)


if __name__ == "__main__":
    run_stdio()

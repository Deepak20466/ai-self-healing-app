"""Entrypoint for the real mcp-pod process (Procfile): streamable HTTP transport.

Run with: `python -m mcp_server.http_main`.
"""

from __future__ import annotations

from mcp_server.server import run_http

if __name__ == "__main__":
    run_http()

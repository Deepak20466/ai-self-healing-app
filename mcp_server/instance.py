"""The single shared MCPServer instance.

Every `tools/*.py` / `resources.py` module imports `mcp` from here to
register itself via decorators; `server.py` imports this module plus every
tool/resource module (for their registration side effects) and exposes the
process entrypoints. Kept separate from `server.py` so tool modules never
need to import `server.py` itself (which would be circular).
"""

from __future__ import annotations

from mcp.server.mcpserver import MCPServer

mcp: MCPServer[None] = MCPServer(
    name="ai-self-healing-mcp",
    instructions=(
        "Tools for an AI-powered self-healing application. Inspect captured "
        "errors and contract violations, read/search code, run tests and "
        "propose patches in an isolated git worktree, and inspect or act on "
        "CI/CD and deployment state. Runtime auto-fixes (for a runtime_error "
        "or contract_violation heal_job) may only modify files under "
        "apps/target_app/ — propose_patch enforces this from the heal_job's "
        "type in the database, not from caller input. Destructive tools "
        "(trigger_rollback) require a confirmation token from an "
        "authenticated chat session."
    ),
)

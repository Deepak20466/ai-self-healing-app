"""healer-pod: the autonomous fix agent (SPEC.md healer-pod section).

Pulls `heal_jobs` off the Postgres queue, runs an agentic loop against the
Anthropic API with the mcp-pod tools (connected as a real MCP client), and —
on success — commits a fix (runtime_error/contract_violation: a new branch
plus PR; ci_failure: a fix commit pushed to the existing PR branch) to
GitHub. The AI chat server is Phase 6.
"""

from __future__ import annotations

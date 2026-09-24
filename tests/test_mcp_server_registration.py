"""Smoke test: every SPEC.md-listed tool and resource is actually registered
on the MCPServer instance.

This is the programmatic equivalent of "tools work via `mcp dev`" — it
verifies the server's own registration is complete and each tool's schema
is derivable, without needing the interactive `mcp dev` inspector.
"""

from __future__ import annotations

from mcp_server.server import mcp

EXPECTED_TOOLS = {
    # errors
    "get_error",
    "list_open_errors",
    "get_contract_violation",
    # code
    "read_file",
    "search_code",
    "list_files",
    "get_git_blame",
    "get_recent_commits",
    "run_tests",
    "propose_patch",
    # deploy
    "get_deployment_status",
    "get_health",
    "get_metrics",
    # cicd
    "list_workflow_runs",
    "get_workflow_run",
    "get_job_logs",
    "rerun_workflow",
    "cancel_workflow",
    "get_pr_status",
    "trigger_rollback",
}

EXPECTED_RESOURCES = {
    "errors://open",
    "repo://tree",
    "pipeline://runs/recent",
}


async def test_every_spec_tool_is_registered() -> None:
    tools = await mcp.list_tools()
    registered_names = {t.name for t in tools}
    assert EXPECTED_TOOLS <= registered_names


async def test_every_spec_resource_is_registered() -> None:
    resources = await mcp.list_resources()
    registered_uris = {str(r.uri) for r in resources}
    assert EXPECTED_RESOURCES <= registered_uris


async def test_every_tool_has_a_usable_input_schema() -> None:
    tools = await mcp.list_tools()
    for tool in tools:
        assert tool.input_schema is not None
        assert tool.input_schema.get("type") == "object"


async def test_destructive_tools_require_a_confirmation_token_parameter() -> None:
    tools = {t.name: t for t in await mcp.list_tools()}
    for name in ("cancel_workflow", "trigger_rollback"):
        properties = tools[name].input_schema.get("properties", {})
        assert "confirmation_token" in properties

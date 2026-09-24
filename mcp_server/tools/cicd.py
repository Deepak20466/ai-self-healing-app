"""CI/CD tools: list_workflow_runs, get_workflow_run, get_job_logs,
rerun_workflow, cancel_workflow, get_pr_status, trigger_rollback.

`cancel_workflow` and `trigger_rollback` require a `confirmation_token`
(SPEC.md SECURITY: "destructive actions (rollback, cancel, merge) require
... an explicit 'yes' confirmation in chat before the tool runs" — verified
here in code, issued by the chat layer once a user actually confirms).
"""

from __future__ import annotations

from typing import Any

from mcp_server.audit import audited_tool
from mcp_server.confirmation import ConfirmationError, verify_confirmation_token
from mcp_server.github_client import GitHubClient, GitHubClientError
from mcp_server.instance import mcp
from mcp_server.log_trim import trim_to_failing_step
from mcp_server.tools._exceptions import ToolError


@audited_tool(mcp, "list_workflow_runs")
async def list_workflow_runs(
    branch: str | None = None, status: str | None = None
) -> list[dict[str, Any]]:
    """List recent GitHub Actions workflow runs, optionally filtered."""
    async with GitHubClient() as client:
        try:
            runs = await client.list_workflow_runs(branch=branch, status=status)
        except GitHubClientError as exc:
            raise ToolError(str(exc)) from exc

    return [
        {
            "run_id": run["id"],
            "workflow_name": run.get("name"),
            "branch": run.get("head_branch"),
            "sha": run.get("head_sha"),
            "status": run.get("status"),
            "conclusion": run.get("conclusion"),
            "html_url": run.get("html_url"),
        }
        for run in runs
    ]


@audited_tool(mcp, "get_workflow_run")
async def get_workflow_run(run_id: int) -> dict[str, Any]:
    """Full detail for one workflow run."""
    async with GitHubClient() as client:
        try:
            run = await client.get_workflow_run(run_id)
        except GitHubClientError as exc:
            raise ToolError(str(exc)) from exc
    return run


@audited_tool(mcp, "get_job_logs")
async def get_job_logs(run_id: int, job_name: str) -> dict[str, Any]:
    """Logs for `job_name` within `run_id`, trimmed to the failing step, capped at 20KB."""
    async with GitHubClient() as client:
        try:
            jobs = await client.list_jobs_for_run(run_id)
            matching = next((j for j in jobs if j.get("name") == job_name), None)
            if matching is None:
                available = [j.get("name") for j in jobs]
                raise ToolError(
                    f"No job named {job_name!r} in run {run_id}. Available: {available}"
                )
            raw_logs = await client.get_job_logs_text(matching["id"])
        except GitHubClientError as exc:
            raise ToolError(str(exc)) from exc

    return {
        "run_id": run_id,
        "job_name": job_name,
        "job_id": matching["id"],
        "conclusion": matching.get("conclusion"),
        "logs": trim_to_failing_step(raw_logs),
    }


@audited_tool(mcp, "rerun_workflow")
async def rerun_workflow(run_id: int, failed_only: bool = True) -> dict[str, Any]:
    """Re-run a workflow (failed jobs only by default)."""
    async with GitHubClient() as client:
        try:
            await client.rerun_workflow(run_id, failed_only=failed_only)
        except GitHubClientError as exc:
            raise ToolError(str(exc)) from exc
    return {"run_id": run_id, "rerun": True, "failed_only": failed_only}


@audited_tool(mcp, "cancel_workflow")
async def cancel_workflow(run_id: int, confirmation_token: str) -> dict[str, Any]:
    """Cancel a running workflow. Destructive: requires a confirmation token."""
    try:
        verify_confirmation_token(confirmation_token, "cancel_workflow")
    except ConfirmationError as exc:
        raise ToolError(str(exc)) from exc

    async with GitHubClient() as client:
        try:
            await client.cancel_workflow(run_id)
        except GitHubClientError as exc:
            raise ToolError(str(exc)) from exc
    return {"run_id": run_id, "cancelled": True}


@audited_tool(mcp, "get_pr_status")
async def get_pr_status(pr_number: int) -> dict[str, Any]:
    """Checks, reviews, and mergeability for a pull request."""
    async with GitHubClient() as client:
        try:
            pr = await client.get_pull_request(pr_number)
            reviews = await client.list_pull_request_reviews(pr_number)
            check_runs = await client.list_check_runs(pr["head"]["sha"])
        except GitHubClientError as exc:
            raise ToolError(str(exc)) from exc

    latest_review_by_user: dict[str, str] = {}
    for review in reviews:
        user = review.get("user", {}).get("login", "unknown")
        latest_review_by_user[user] = review.get("state", "")

    return {
        "pr_number": pr_number,
        "state": pr.get("state"),
        "mergeable": pr.get("mergeable"),
        "mergeable_state": pr.get("mergeable_state"),
        "reviews": latest_review_by_user,
        "checks": [
            {"name": c.get("name"), "status": c.get("status"), "conclusion": c.get("conclusion")}
            for c in check_runs
        ],
    }


@audited_tool(mcp, "trigger_rollback")
async def trigger_rollback(env: str, confirmation_token: str) -> dict[str, Any]:
    """Dispatch the rollback workflow for `env`. Requires a confirmation token."""
    try:
        verify_confirmation_token(confirmation_token, "trigger_rollback")
    except ConfirmationError as exc:
        raise ToolError(str(exc)) from exc

    async with GitHubClient() as client:
        try:
            await client.dispatch_workflow("rollback.yml", ref="main", inputs={"env": env})
        except GitHubClientError as exc:
            raise ToolError(str(exc)) from exc

    return {"env": env, "rollback_dispatched": True}

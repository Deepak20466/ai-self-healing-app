"""Full-suite gate shared by every AI backend before a fix PR is opened.

A regression test passing proves the bug is fixed; it says nothing about what
the patch broke elsewhere. So after the targeted test passes, every backend
runs the app's whole configured suite (`run_tests` with no `test_path`: pytest
over the worktree for Python apps, the app's own `test_command` otherwise) and
an attempt only counts as a success if that passes too. The evidence is
folded into the attempt's `test_output`, which is what lands in the PR body.
"""

from __future__ import annotations

from typing import Any

from healer.mcp_client import MCPToolClient


async def run_full_suite(
    mcp: MCPToolClient, *, worktree_name: str, heal_job_id: int | None
) -> tuple[bool, str]:
    """Run the app's full suite in the worktree; returns (passed, output tail)."""
    args: dict[str, Any] = {"worktree": worktree_name}
    if heal_job_id is not None:
        args["heal_job_id"] = heal_job_id
    result = await mcp.call_tool("run_tests", args)
    if not isinstance(result, dict):
        return False, str(result)[-4000:]
    return bool(result.get("passed")), str(result.get("output", ""))[-3000:]


def combine_evidence(
    regression_output: str, *, suite_passed: bool | None, suite_output: str = ""
) -> str:
    """Regression + full-suite output as one PR-body-ready block."""
    if suite_passed is None:
        return regression_output
    verdict = "PASSED" if suite_passed else "FAILED"
    return (
        f"Regression test:\n{regression_output[-1000:]}\n\n"
        f"Full test suite: {verdict}\n{suite_output}"
    )

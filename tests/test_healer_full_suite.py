"""The full-suite gate: a fix that passes its regression test but breaks any
other test must fail the attempt, and the PR evidence must include the suite."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from healer import agent_free, full_suite
from healer.agent_free import CLIResult


class FakeMCP:
    def __init__(self, *, regression_passes: bool, suite_passes: bool) -> None:
        self.regression_passes = regression_passes
        self.suite_passes = suite_passes
        self.calls: list[dict[str, Any]] = []

    async def call_tool(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        assert name == "run_tests"
        self.calls.append(args)
        if "test_path" in args:
            return {"passed": self.regression_passes, "output": "1 regression run"}
        return {"passed": self.suite_passes, "output": "full suite output"}


def _cli_result() -> CLIResult:
    return CLIResult(
        result_text="fixed it",
        is_error=False,
        subtype="success",
        num_turns=1,
        session_id=None,
        input_tokens=0,
        output_tokens=0,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=0,
        total_cost_usd=None,
        raw={},
    )


@pytest.fixture
def real_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(agent_free, "run_full_suite", full_suite.run_full_suite)

    async def _stat(*, cwd: Path) -> str:
        return " apps/x.py | 2 +-"

    async def _path(_cwd: Path) -> str:
        return "apps/test_x.py"

    monkeypatch.setattr(agent_free.git_utils, "diff_stat", _stat)
    monkeypatch.setattr(agent_free, "_regression_test_path", _path)


async def _verify(mcp: FakeMCP) -> agent_free._AttemptOutcome:
    return await agent_free._verify_and_summarize(
        mcp=mcp,  # type: ignore[arg-type]
        worktree_path=Path("."),
        worktree_name="heal-1",
        cli_result=_cli_result(),
        heal_job_id=7,
    )


@pytest.mark.usefixtures("real_gate")
async def test_breaking_another_test_fails_the_attempt() -> None:
    mcp = FakeMCP(regression_passes=True, suite_passes=False)
    outcome = await _verify(mcp)
    assert outcome.success is False
    assert "Full test suite: FAILED" in outcome.test_output
    assert mcp.calls[-1] == {"worktree": "heal-1", "heal_job_id": 7}


@pytest.mark.usefixtures("real_gate")
async def test_full_suite_evidence_lands_in_the_pr_body_text() -> None:
    outcome = await _verify(FakeMCP(regression_passes=True, suite_passes=True))
    assert outcome.success is True
    assert "Full test suite: PASSED" in outcome.test_output
    assert "full suite output" in outcome.test_output


@pytest.mark.usefixtures("real_gate")
async def test_failing_regression_test_skips_the_suite() -> None:
    mcp = FakeMCP(regression_passes=False, suite_passes=True)
    outcome = await _verify(mcp)
    assert outcome.success is False
    assert all("test_path" in c for c in mcp.calls)


async def test_run_full_suite_passes_no_test_path() -> None:
    mcp = FakeMCP(regression_passes=True, suite_passes=True)
    passed, out = await full_suite.run_full_suite(mcp, worktree_name="w", heal_job_id=None)  # type: ignore[arg-type]
    assert passed and out == "full suite output"
    assert mcp.calls == [{"worktree": "w"}]

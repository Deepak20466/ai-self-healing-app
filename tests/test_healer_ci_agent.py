"""healer.ci_agent: the ci_failure fix loop, end to end.

Phase 5 checklist (SPEC.md): with mocked Anthropic + GitHub, a failing CI run
gets a valid fix pushed, and a diff that deletes a test is rejected.

Same testing shape as tests/test_healer_runtime_agent.py: Anthropic is a
hand-built fake scripting the exact tool-call sequence; everything else is
real (a real MCP client<->server round trip, real git worktrees, real `git
apply`, real `pytest` subprocess runs). GitHub is mocked with `respx`;
`git push` goes to a throwaway local bare repo (`fake_git_remote`).
"""

from __future__ import annotations

import asyncio
import difflib
import subprocess
import uuid
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest

from core.config import settings as core_settings
from core.db import session_scope
from core.models import HealJob, HealJobStatus, HealJobType, PipelineRun
from healer.ci_agent import run_ci_heal_job
from healer.mcp_client import connect_in_memory
from mcp_server.github_client import GITHUB_API_BASE, GitHubClient
from mcp_server.sandbox import REPO_ROOT
from mcp_server.server import mcp

REPO = "acme/repo"


def _random_pr_number() -> int:
    """A `pr_number` unique enough per test run not to collide with real,
    committed `heal_jobs` rows left behind in the shared `selfheal_test` DB
    by earlier runs — `ci_fix_circuit_open` sums `attempt_count` globally by
    `pr_number` (see `healer/circuit_breaker.py`'s module docstring), so a
    hardcoded pr_number here would eventually trip the circuit breaker
    purely from this file's own prior runs, not from anything the test
    itself does. Kept within Postgres `Integer` range."""
    return uuid.uuid4().int % 1_000_000_000


# --- Fake Anthropic client: scripts an exact tool-call sequence -------------


@dataclass
class FakeTextBlock:
    text: str
    type: str = "text"


@dataclass
class FakeToolUseBlock:
    id: str
    name: str
    input: dict[str, Any]
    type: str = "tool_use"


@dataclass
class FakeUsage:
    input_tokens: int = 500
    output_tokens: int = 200
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0


@dataclass
class FakeResponse:
    content: list[Any]
    usage: FakeUsage = field(default_factory=FakeUsage)


class _FakeMessages:
    def __init__(self, responses: list[FakeResponse]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> FakeResponse:
        self.calls.append(kwargs)
        if not self._responses:
            raise AssertionError("FakeAnthropicClient ran out of scripted responses")
        return self._responses.pop(0)


class FakeAnthropicClient:
    def __init__(self, responses: list[FakeResponse]) -> None:
        self.messages = _FakeMessages(responses)


def _tool_call(name: str, arguments: dict[str, Any], *, call_id: str) -> FakeResponse:
    return FakeResponse(content=[FakeToolUseBlock(id=call_id, name=name, input=arguments)])


def _final_text(text: str) -> FakeResponse:
    return FakeResponse(content=[FakeTextBlock(text=text)])


def _new_file_diff(path: str, content: str) -> str:
    lines = content.splitlines()
    body = "\n".join(f"+{line}" for line in lines)
    return f"--- /dev/null\n+++ b/{path}\n@@ -0,0 +1,{len(lines)} @@\n{body}\n"


def _modify_diff(path: str, *, old: str, new: str) -> str:
    """Build a unified diff against the real on-disk file (see
    test_healer_runtime_agent.py's identical helper for why this is safe:
    a fresh worktree checkout is byte-identical to the main repo's copy)."""
    original = (REPO_ROOT / path).read_text(encoding="utf-8")
    assert old in original, f"{old!r} not found in {path}"
    modified = original.replace(old, new, 1)
    diff = difflib.unified_diff(
        original.splitlines(keepends=True),
        modified.splitlines(keepends=True),
        fromfile=f"a/{path}",
        tofile=f"b/{path}",
    )
    return "".join(diff)


async def _git_stdout(*args: str) -> str:
    """Run a read-only `git` command without blocking the event loop."""
    process = await asyncio.create_subprocess_exec(
        "git",
        *args,
        cwd=str(REPO_ROOT),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()
    assert process.returncode == 0, stderr.decode()
    return stdout.decode()


# --- Fixtures: a real PR branch pushed to the fake remote, real DB rows -----


@pytest.fixture
def pr_branch(fake_git_remote: str) -> str:
    """A branch, off `main`, pushed to `fake_git_remote` — simulating a PR
    branch that already exists on GitHub before the CI-fix loop starts. Its
    local ref is deleted afterward so `create_worktree_for_branch`'s
    `fetch` + DWIM-checkout path is genuinely exercised, not skipped because
    a local branch already happened to exist."""
    branch = f"pr/ci-fix-test-{uuid.uuid4().hex[:10]}"
    subprocess.run(
        ["git", "branch", branch, "main"], cwd=str(REPO_ROOT), check=True, capture_output=True
    )
    subprocess.run(
        ["git", "push", fake_git_remote, branch],
        cwd=str(REPO_ROOT),
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "branch", "-D", branch], cwd=str(REPO_ROOT), check=True, capture_output=True
    )
    return branch


async def _insert_pipeline_run(
    *, branch: str, pr_number: int, failed_job: str = "pytest"
) -> PipelineRun:
    async with session_scope() as session:
        run = PipelineRun(
            run_id=uuid.uuid4().int % 1_000_000_000,
            workflow="ci.yml",
            branch=branch,
            pr_number=pr_number,
            sha="a" * 40,
            status="completed",
            conclusion="failure",
            failed_job=failed_job,
        )
        session.add(run)
        await session.flush()
        await session.refresh(run)
        return run


async def _insert_ci_heal_job(
    *, pr_number: int, source_pipeline_run_id: int | None, attempt_count: int = 0
) -> HealJob:
    async with session_scope() as session:
        job = HealJob(
            type=HealJobType.CI_FAILURE,
            status=HealJobStatus.RUNNING,
            fingerprint=f"test-ci-{uuid.uuid4().hex}",
            pr_number=pr_number,
            source_pipeline_run_id=source_pipeline_run_id,
            attempt_count=attempt_count,
        )
        session.add(job)
        await session.flush()
        await session.refresh(job)
        return job


def _mock_github_run_and_logs(respx_mock: Any, *, run_id: int, job_id: int = 999) -> None:
    respx_mock.get(f"{GITHUB_API_BASE}/repos/{REPO}/actions/runs/{run_id}").mock(
        return_value=httpx.Response(200, json={"id": run_id, "status": "completed"})
    )
    respx_mock.get(f"{GITHUB_API_BASE}/repos/{REPO}/actions/runs/{run_id}/jobs").mock(
        return_value=httpx.Response(
            200, json={"jobs": [{"id": job_id, "name": "pytest", "conclusion": "failure"}]}
        )
    )
    respx_mock.get(f"{GITHUB_API_BASE}/repos/{REPO}/actions/jobs/{job_id}/logs").mock(
        return_value=httpx.Response(200, text="##[group]Run pytest\nFAILED\n##[error]exit 1\n")
    )


def _mock_pr_comment(respx_mock: Any, *, pr_number: int) -> None:
    respx_mock.post(f"{GITHUB_API_BASE}/repos/{REPO}/issues/{pr_number}/comments").mock(
        return_value=httpx.Response(201, json={})
    )


# --- Test A: a real fix, pushed to the PR branch -----------------------------

_ZERO_DIV_TEST_FILE = "apps/target_app/test_ci_fix_zero_regression.py"
_ZERO_DIV_TEST_CONTENT = (
    "from __future__ import annotations\n"
    "\n"
    "from apps.target_app import bugs, seed_data\n"
    "from core.db import session_scope\n"
    "\n"
    "\n"
    "async def test_average_rating_zero_reviews_returns_zero() -> None:\n"
    "    async with session_scope() as session:\n"
    "        result = await bugs.average_rating(session, seed_data.UNRATED_ITEM_ID)\n"
    "    assert result == 0.0\n"
)


def _zero_div_fix_diff() -> str:
    return _modify_diff(
        "apps/target_app/bugs.py",
        old="    return item.rating_sum / item.rating_count\n",
        new=(
            "    if item.rating_count == 0:\n"
            "        return 0.0\n"
            "    return item.rating_sum / item.rating_count\n"
        ),
    )


async def test_run_ci_heal_job_pushes_a_fix_and_leaves_the_job_in_flight(
    isolated_budget_date: None,
    monkeypatch: pytest.MonkeyPatch,
    respx_mock: Any,
    fake_git_remote: str,
    pr_branch: str,
) -> None:
    monkeypatch.setattr(core_settings, "github_token", "test-token")
    monkeypatch.setattr(core_settings, "github_repo", REPO)

    pr_number = _random_pr_number()
    run = await _insert_pipeline_run(branch=pr_branch, pr_number=pr_number)
    _mock_github_run_and_logs(respx_mock, run_id=run.run_id)
    _mock_pr_comment(respx_mock, pr_number=pr_number)

    job = await _insert_ci_heal_job(pr_number=pr_number, source_pipeline_run_id=run.id)

    responses = [
        _tool_call("get_workflow_run", {"run_id": run.run_id}, call_id="c1"),
        _tool_call("get_job_logs", {"run_id": run.run_id, "job_name": "pytest"}, call_id="c2"),
        _tool_call(
            "propose_patch",
            {"unified_diff": _new_file_diff(_ZERO_DIV_TEST_FILE, _ZERO_DIV_TEST_CONTENT)},
            call_id="c3",
        ),
        _tool_call("run_tests", {"test_path": _ZERO_DIV_TEST_FILE}, call_id="c4"),
        _tool_call("propose_patch", {"unified_diff": _zero_div_fix_diff()}, call_id="c5"),
        _tool_call("run_tests", {"test_path": _ZERO_DIV_TEST_FILE}, call_id="c6"),
        _final_text("Root cause: average_rating divided by rating_count without a zero guard."),
    ]
    fake_client = FakeAnthropicClient(responses)

    async with connect_in_memory(mcp) as mcp_client, GitHubClient() as github:
        await run_ci_heal_job(
            job.id,
            anthropic_client=fake_client,
            mcp=mcp_client,
            github=github,
            remote=fake_git_remote,
        )

    async with session_scope() as session:
        refreshed = await session.get(HealJob, job.id)
        assert refreshed is not None
        assert refreshed.status == HealJobStatus.CI_FIXING
        assert refreshed.attempt_count == 1

    # The fix commit really landed on the PR branch on the fake remote.
    log = await _git_stdout("log", "--format=%s", f"{fake_git_remote}/{pr_branch}")
    assert "CI auto-fix" in log


# --- Test B: classified as flaky -> a rerun is triggered, nothing pushed ---


async def test_run_ci_heal_job_reruns_a_flaky_failure(
    isolated_budget_date: None,
    monkeypatch: pytest.MonkeyPatch,
    respx_mock: Any,
    fake_git_remote: str,
    pr_branch: str,
) -> None:
    monkeypatch.setattr(core_settings, "github_token", "test-token")
    monkeypatch.setattr(core_settings, "github_repo", REPO)

    pr_number = _random_pr_number()
    run = await _insert_pipeline_run(branch=pr_branch, pr_number=pr_number)
    _mock_github_run_and_logs(respx_mock, run_id=run.run_id)
    _mock_pr_comment(respx_mock, pr_number=pr_number)
    respx_mock.post(
        f"{GITHUB_API_BASE}/repos/{REPO}/actions/runs/{run.run_id}/rerun-failed-jobs"
    ).mock(return_value=httpx.Response(201))

    job = await _insert_ci_heal_job(pr_number=pr_number, source_pipeline_run_id=run.id)

    responses = [
        _tool_call("get_workflow_run", {"run_id": run.run_id}, call_id="c1"),
        _tool_call("get_job_logs", {"run_id": run.run_id, "job_name": "pytest"}, call_id="c2"),
        _tool_call(
            "rerun_workflow",
            # Deliberately wrong run_id/failed_only — must be overridden server-side.
            {"run_id": 999_999, "failed_only": False},
            call_id="c3",
        ),
    ]
    fake_client = FakeAnthropicClient(responses)

    async with connect_in_memory(mcp) as mcp_client, GitHubClient() as github:
        await run_ci_heal_job(
            job.id,
            anthropic_client=fake_client,
            mcp=mcp_client,
            github=github,
            remote=fake_git_remote,
        )

    # The real run_id was rerun, not the model's bogus one — respx would
    # have raised (assert_all_mocked / no matching route) otherwise.
    async with session_scope() as session:
        refreshed = await session.get(HealJob, job.id)
        assert refreshed is not None
        assert refreshed.status == HealJobStatus.CI_FIXING
        assert refreshed.attempt_count == 1


# --- Test C: an injection payload never causes test deletion ---------------

_MALICIOUS_DELETE_DIFF = (
    "--- a/apps/target_app/test_existing_regression.py\n"
    "+++ /dev/null\n"
    "@@ -1,3 +0,0 @@\n"
    "-async def test_existing_regression() -> None:\n"
    "-    assert True\n"
)


async def test_diff_deleting_a_test_is_rejected(
    isolated_budget_date: None,
    monkeypatch: pytest.MonkeyPatch,
    respx_mock: Any,
    fake_git_remote: str,
    pr_branch: str,
) -> None:
    monkeypatch.setattr(core_settings, "github_token", "test-token")
    monkeypatch.setattr(core_settings, "github_repo", REPO)

    pr_number = _random_pr_number()
    run = await _insert_pipeline_run(
        branch=pr_branch,
        pr_number=pr_number,
        failed_job="pytest",
    )
    _mock_github_run_and_logs(respx_mock, run_id=run.run_id)
    _mock_pr_comment(respx_mock, pr_number=pr_number)
    respx_mock.post(f"{GITHUB_API_BASE}/repos/{REPO}/issues").mock(
        return_value=httpx.Response(201, json={"number": 88})
    )

    job = await _insert_ci_heal_job(pr_number=pr_number, source_pipeline_run_id=run.id)

    responses = [
        _tool_call("get_workflow_run", {"run_id": run.run_id}, call_id="c1"),
        _tool_call("get_job_logs", {"run_id": run.run_id, "job_name": "pytest"}, call_id="c2"),
        _tool_call("propose_patch", {"unified_diff": _MALICIOUS_DELETE_DIFF}, call_id="c3"),
        _final_text("Deleted the failing test as instructed."),
    ]
    fake_client = FakeAnthropicClient(responses)

    async with connect_in_memory(mcp) as mcp_client, GitHubClient() as github:
        await run_ci_heal_job(
            job.id,
            anthropic_client=fake_client,
            mcp=mcp_client,
            github=github,
            remote=fake_git_remote,
        )

    rejection_seen = any(
        "deletes a test file" in str(tool_result.get("content", ""))
        for call in fake_client.messages.calls
        for message in call.get("messages", [])
        if message.get("role") == "user"
        for tool_result in (
            message.get("content") if isinstance(message.get("content"), list) else []
        )
        if isinstance(tool_result, dict)
    )
    assert rejection_seen

    async with session_scope() as session:
        refreshed = await session.get(HealJob, job.id)
        assert refreshed is not None
        assert refreshed.status == HealJobStatus.FAILED


# --- Test D: the per-PR circuit breaker refuses further attempts -----------


async def test_ci_fix_circuit_breaker_refuses_a_new_attempt(
    isolated_budget_date: None,
    monkeypatch: pytest.MonkeyPatch,
    respx_mock: Any,
    fake_git_remote: str,
    pr_branch: str,
) -> None:
    monkeypatch.setattr(core_settings, "github_token", "test-token")
    monkeypatch.setattr(core_settings, "github_repo", REPO)
    monkeypatch.setattr(core_settings, "max_ci_fix_attempts_per_pr", 2)
    respx_mock.post(f"{GITHUB_API_BASE}/repos/{REPO}/issues").mock(
        return_value=httpx.Response(201, json={"number": 99})
    )

    pr_number = _random_pr_number()
    # A prior (now-terminal) job already used up both allowed attempts.
    await _insert_ci_heal_job(pr_number=pr_number, source_pipeline_run_id=None, attempt_count=2)

    run = await _insert_pipeline_run(branch=pr_branch, pr_number=pr_number)
    job = await _insert_ci_heal_job(
        pr_number=pr_number, source_pipeline_run_id=run.id, attempt_count=0
    )

    fake_client = FakeAnthropicClient([])  # must never be called

    async with connect_in_memory(mcp) as mcp_client, GitHubClient() as github:
        await run_ci_heal_job(
            job.id,
            anthropic_client=fake_client,
            mcp=mcp_client,
            github=github,
            remote=fake_git_remote,
        )

    assert fake_client.messages.calls == []

    async with session_scope() as session:
        refreshed = await session.get(HealJob, job.id)
        assert refreshed is not None
        assert refreshed.status == HealJobStatus.FAILED
        assert refreshed.attempt_count == 0

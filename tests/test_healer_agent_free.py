"""healer.agent_free: the Claude Code CLI backend.

`run_claude_cli` is tested against a mocked subprocess (no real CLI calls in
pytest, per CLAUDE.md) covering: success, timeout, a JSON `is_error` result,
"not logged in", "usage limit", CLI-not-found, and malformed (non-JSON)
output. The higher-level loops (`run_heal_job_free`/`run_ci_heal_job_free`)
are tested end to end against a real MCP server, real git worktrees and a
throwaway git remote, with `run_claude_cli` itself monkeypatched to a fake
that (like the real CLI would) performs its "edits" via real `git apply`/
`mcp.call_tool` calls before returning its scripted `CLIResult` — see
CLAUDE.md "Anthropic and GitHub are always mocked in tests" (the same
principle applies to the CLI subprocess in free mode).
"""

from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import select

from core.config import settings as core_settings
from core.db import session_scope
from core.models import AuditLog, Error, HealJob, HealJobStatus, HealJobType, OpenResolvedStatus
from healer import agent_free
from healer.agent_free import (
    ClaudeCLIMalformedOutputError,
    ClaudeCLINotFoundError,
    ClaudeCLINotLoggedInError,
    ClaudeCLITimeoutError,
    ClaudeCLIUsageLimitError,
    CLIResult,
    run_claude_cli,
    run_heal_job_free,
)
from healer.mcp_client import connect_in_memory
from mcp_server.github_client import GITHUB_API_BASE, GitHubClient
from mcp_server.sandbox import REPO_ROOT
from mcp_server.server import mcp


@pytest.fixture(autouse=True)
def _deterministic_cli_resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force `run_claude_cli`'s "claude" -> bare-name, non-Windows-shim path
    by default, so these tests exercise `create_subprocess_exec` (the branch
    every scripted `_FakeExec` below patches) regardless of whether the
    machine actually running pytest happens to have a real `claude.cmd`/
    `claude.exe`/POSIX `claude` on PATH. The one test that specifically
    exercises the Windows `.cmd`-shim `create_subprocess_shell` branch
    overrides this itself.
    """
    monkeypatch.setattr("healer.agent_free.shutil.which", lambda _name: None)


# --- A fake asyncio subprocess, for run_claude_cli's own unit tests ---------


@dataclass
class _FakeProcess:
    stdout_bytes: bytes = b""
    stderr_bytes: bytes = b""
    returncode: int = 0
    pid: int = 4242
    hang: bool = False
    killed: bool = False

    async def communicate(self, input: bytes | None = None) -> tuple[bytes, bytes]:  # noqa: A002
        self.sent_stdin = input
        if self.hang:
            await asyncio.sleep(9999)
        return self.stdout_bytes, self.stderr_bytes

    def kill(self) -> None:
        self.killed = True

    async def wait(self) -> int:
        return self.returncode


@dataclass
class _FakeExec:
    """Patches `asyncio.create_subprocess_exec`, recording exactly how it was called."""

    process: _FakeProcess | None
    exc: Exception | None = None
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def __call__(self, *args: Any, **kwargs: Any) -> _FakeProcess:
        self.calls.append({"args": args, "kwargs": kwargs})
        if self.exc is not None:
            raise self.exc
        assert self.process is not None
        return self.process


def _success_stdout(
    *, result: str = "Fixed it.", num_turns: int = 4, total_cost_usd: float | None = None
) -> bytes:
    payload: dict[str, Any] = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "num_turns": num_turns,
        "result": result,
        "session_id": "sess-1",
        "usage": {
            "input_tokens": 1000,
            "output_tokens": 200,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
        },
    }
    if total_cost_usd is not None:
        payload["total_cost_usd"] = total_cost_usd
    return json.dumps(payload).encode("utf-8")


async def test_run_claude_cli_success_parses_result_and_usage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeExec(process=_FakeProcess(stdout_bytes=_success_stdout(total_cost_usd=0.0)))
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake)

    result = await run_claude_cli("do the thing", cwd=REPO_ROOT, max_turns=5, timeout_s=30)

    assert isinstance(result, CLIResult)
    assert result.result_text == "Fixed it."
    assert result.is_error is False
    assert result.num_turns == 4
    assert result.input_tokens == 1000
    assert result.total_cost_usd == Decimal("0.0")

    assert len(fake.calls) == 1
    argv = list(fake.calls[0]["args"])
    assert argv[0] == "claude"
    assert argv[1:8] == [
        "-p",
        "--output-format",
        "json",
        "--mcp-config",
        str(REPO_ROOT / ".mcp.json"),
        "--strict-mcp-config",
        "--allowedTools",
    ]
    assert argv[8] == "mcp__selfheal__*"
    assert argv[9] == "--disallowedTools"
    assert argv[10] == "Bash,Edit,Write,MultiEdit,NotebookEdit,WebFetch,WebSearch"
    assert argv[11:] == ["--max-turns", "5"]

    kwargs = fake.calls[0]["kwargs"]
    assert kwargs["cwd"] == str(REPO_ROOT)
    assert kwargs["stdin"] == asyncio.subprocess.PIPE

    # Prompt goes via stdin, never argv.
    assert "do the thing" not in argv
    assert fake.process is not None
    assert fake.process.sent_stdin == b"do the thing"

    # ANTHROPIC_* vars never reach the CLI subprocess.
    env = kwargs["env"]
    stripped_keys = (
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_MODEL",
    )
    for key in stripped_keys:
        assert key not in env


async def test_run_claude_cli_wraps_a_windows_cmd_shim_correctly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`asyncio.create_subprocess_exec` can't launch a `.cmd`/`.bat` file
    directly on Windows (no shell, no PATHEXT resolution) — this is the real
    shape `shutil.which("claude")` resolves to for an npm-installed CLI on
    Windows, so that case goes through `create_subprocess_shell` instead.
    Verifies the command line quotes just the path (a path with a space is
    the real-world case: `C:\\Users\\Someone Spaced\\...\\claude.cmd`) with
    no extra outer wrapping — confirmed directly against both `asyncio.
    create_subprocess_shell` and `subprocess.run(shell=True)` that this exact
    form is what Windows' cmd.exe actually parses correctly; an earlier,
    "helpfully" double-quote-wrapped version of this looked more defensive
    but silently broke it instead.
    """
    monkeypatch.setattr(agent_free.sys, "platform", "win32")
    monkeypatch.setattr(
        "healer.agent_free.shutil.which", lambda _name: r"C:\fake dir\npm\claude.cmd"
    )
    fake = _FakeExec(process=_FakeProcess(stdout_bytes=_success_stdout()))
    monkeypatch.setattr(asyncio, "create_subprocess_shell", fake)

    await run_claude_cli("prompt", cwd=REPO_ROOT)

    assert len(fake.calls) == 1
    (command_line,) = fake.calls[0]["args"]
    assert command_line == (
        '"C:\\fake dir\\npm\\claude.cmd" -p --output-format json --mcp-config '
        f'"{REPO_ROOT / ".mcp.json"}" --strict-mcp-config --allowedTools '
        "mcp__selfheal__* --disallowedTools "
        "Bash,Edit,Write,MultiEdit,NotebookEdit,WebFetch,WebSearch --max-turns 30"
    )


async def test_run_claude_cli_does_not_shell_wrap_a_plain_exe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A real `.exe` (or a POSIX `claude` with no extension) never needs the
    cmd.exe workaround — it goes through the plain, argv-list
    `create_subprocess_exec` path like any normal program."""
    monkeypatch.setattr(agent_free.sys, "platform", "win32")
    monkeypatch.setattr("healer.agent_free.shutil.which", lambda _name: r"C:\fake\npm\claude.exe")
    fake = _FakeExec(process=_FakeProcess(stdout_bytes=_success_stdout()))
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake)

    await run_claude_cli("prompt", cwd=REPO_ROOT)

    argv = list(fake.calls[0]["args"])
    assert argv[0] == r"C:\fake\npm\claude.exe"
    assert argv[1] == "-p"


async def test_run_claude_cli_strips_anthropic_env_vars_present_in_process_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-not-leak")
    monkeypatch.setenv("ANTHROPIC_MODEL", "should-not-leak-either")
    fake = _FakeExec(process=_FakeProcess(stdout_bytes=_success_stdout()))
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake)

    await run_claude_cli("prompt", cwd=REPO_ROOT)

    env = fake.calls[0]["kwargs"]["env"]
    assert "ANTHROPIC_API_KEY" not in env
    assert "ANTHROPIC_MODEL" not in env


async def test_run_claude_cli_is_error_result_still_parses(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeExec(
        process=_FakeProcess(
            stdout_bytes=_success_stdout(result="Could not find a fix."),
        )
    )
    # Simulate is_error=True without matching the not-logged-in/usage-limit patterns.
    payload = json.loads(fake.process.stdout_bytes)  # type: ignore[union-attr]
    payload["is_error"] = True
    fake.process.stdout_bytes = json.dumps(payload).encode("utf-8")  # type: ignore[union-attr]
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake)

    result = await run_claude_cli("prompt", cwd=REPO_ROOT)
    assert result.is_error is True
    assert result.result_text == "Could not find a fix."


async def test_run_claude_cli_timeout_kills_process_tree(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeExec(process=_FakeProcess(hang=True))
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake)

    kill_calls: list[int] = []

    async def fake_kill_tree(pid: int) -> None:
        kill_calls.append(pid)

    monkeypatch.setattr(agent_free, "_kill_process_tree", fake_kill_tree)

    with pytest.raises(ClaudeCLITimeoutError):
        await run_claude_cli("prompt", cwd=REPO_ROOT, timeout_s=0.01)

    assert kill_calls == [fake.process.pid]  # type: ignore[union-attr]


async def test_run_claude_cli_not_found_raises_clean_error(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeExec(process=None, exc=FileNotFoundError("no such file"))
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake)

    with pytest.raises(ClaudeCLINotFoundError):
        await run_claude_cli("prompt", cwd=REPO_ROOT)


async def test_run_claude_cli_not_logged_in_detected_from_stderr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeExec(
        process=_FakeProcess(
            stdout_bytes=b"",
            stderr_bytes=b"Error: not logged in. Please run `claude /login` first.",
            returncode=1,
        )
    )
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake)

    with pytest.raises(ClaudeCLINotLoggedInError):
        await run_claude_cli("prompt", cwd=REPO_ROOT)


async def test_run_claude_cli_usage_limit_detected_from_stderr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeExec(
        process=_FakeProcess(
            stdout_bytes=b"",
            stderr_bytes=b"Claude usage limit reached. Your limit resets at 5pm.",
            returncode=1,
        )
    )
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake)

    with pytest.raises(ClaudeCLIUsageLimitError):
        await run_claude_cli("prompt", cwd=REPO_ROOT)


async def test_run_claude_cli_malformed_json_raises_clean_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeExec(process=_FakeProcess(stdout_bytes=b"not json at all {{{", returncode=1))
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake)

    with pytest.raises(ClaudeCLIMalformedOutputError):
        await run_claude_cli("prompt", cwd=REPO_ROOT)


# --- Higher-level loop: run_claude_cli monkeypatched, everything else real --


async def _insert_error(
    *, exception_type: str, file_path: str, line_number: int, function_name: str
) -> int:
    async with session_scope() as session:
        error = Error(
            fingerprint=f"test-{uuid.uuid4().hex}",
            exception_type=exception_type,
            message=f"{exception_type} for test",
            traceback="Traceback (most recent call last):\n  ...",
            file_path=file_path,
            line_number=line_number,
            function_name=function_name,
            status=OpenResolvedStatus.OPEN,
        )
        session.add(error)
        await session.flush()
        return error.id


async def _insert_heal_job(*, job_type: HealJobType, source_error_id: int) -> HealJob:
    async with session_scope() as session:
        job = HealJob(
            type=job_type,
            status=HealJobStatus.RUNNING,
            fingerprint=f"test-{uuid.uuid4().hex}",
            source_error_id=source_error_id,
        )
        session.add(job)
        await session.flush()
        await session.refresh(job)
        return job


def _mock_github_pr_and_labels(respx_mock: Any, *, pr_number: int) -> None:
    respx_mock.post(f"{GITHUB_API_BASE}/repos/acme/repo/pulls").mock(
        return_value=httpx.Response(
            201, json={"number": pr_number, "html_url": "https://example/pr"}
        )
    )
    respx_mock.post(f"{GITHUB_API_BASE}/repos/acme/repo/issues/{pr_number}/labels").mock(
        return_value=httpx.Response(200, json=[])
    )


def _mock_github_issue(respx_mock: Any, *, issue_number: int) -> None:
    respx_mock.post(f"{GITHUB_API_BASE}/repos/acme/repo/issues").mock(
        return_value=httpx.Response(201, json={"number": issue_number})
    )


_TEST_FILE = "apps/target_app/test_average_rating_zero_regression_free.py"
_TEST_FILE_CONTENT = (
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


def _fix_diff() -> str:
    original = (REPO_ROOT / "apps/target_app/bugs.py").read_text(encoding="utf-8")
    old = "    return item.rating_sum / item.rating_count\n"
    assert old in original
    new = (
        "    if item.rating_count == 0:\n"
        "        return 0.0\n"
        "    return item.rating_sum / item.rating_count\n"
    )
    import difflib

    diff = difflib.unified_diff(
        original.splitlines(keepends=True),
        original.replace(old, new, 1).splitlines(keepends=True),
        fromfile="a/apps/target_app/bugs.py",
        tofile="b/apps/target_app/bugs.py",
    )
    return "".join(diff)


def _new_file_diff(path: str, content: str) -> str:
    lines = content.splitlines()
    body = "\n".join(f"+{line}" for line in lines)
    return f"--- /dev/null\n+++ b/{path}\n@@ -0,0 +1,{len(lines)} @@\n{body}\n"


async def test_run_heal_job_free_fixes_zero_division_error_end_to_end(
    isolated_budget_date: None,
    isolated_cli_call_date: None,
    monkeypatch: pytest.MonkeyPatch,
    respx_mock: Any,
    fake_git_remote: str,
) -> None:
    """The CLI's own internal tool calls aren't observable, so the fake here
    stands in for "what a real CLI run would have done": it applies the test
    file, then the fix, straight to the worktree (via the real, sandboxed
    propose_patch MCP tool — proving the guardrails still run) before
    returning a scripted successful CLIResult. `run_heal_job_free`'s own
    post-hoc verification (re-running the real test suite) is what actually
    proves the fix, not this fake's claim."""
    monkeypatch.setattr(core_settings, "github_token", "test-token")
    monkeypatch.setattr(core_settings, "github_repo", "acme/repo")
    _mock_github_pr_and_labels(respx_mock, pr_number=301)
    _mock_github_issue(respx_mock, issue_number=1)

    error_id = await _insert_error(
        exception_type="ZeroDivisionError",
        file_path="apps/target_app/bugs.py",
        line_number=32,
        function_name="average_rating",
    )
    job = await _insert_heal_job(job_type=HealJobType.RUNTIME_ERROR, source_error_id=error_id)

    async def fake_run_claude_cli(prompt: str, *, cwd: Path, **kwargs: Any) -> CLIResult:
        from mcp_server import git_utils

        await git_utils.apply_diff(_new_file_diff(_TEST_FILE, _TEST_FILE_CONTENT), cwd=cwd)
        await git_utils.apply_diff(_fix_diff(), cwd=cwd)
        return CLIResult(
            result_text="Root cause: missing zero guard. Fixed.",
            is_error=False,
            subtype="success",
            num_turns=6,
            session_id="sess-x",
            input_tokens=1200,
            output_tokens=300,
            cache_creation_input_tokens=0,
            cache_read_input_tokens=0,
            total_cost_usd=None,
            raw={},
        )

    monkeypatch.setattr(agent_free, "run_claude_cli", fake_run_claude_cli)

    async with connect_in_memory(mcp) as mcp_client, GitHubClient() as github:
        await run_heal_job_free(job.id, mcp=mcp_client, github=github, remote=fake_git_remote)

    async with session_scope() as session:
        refreshed = await session.get(HealJob, job.id)
        assert refreshed is not None
        assert refreshed.status == HealJobStatus.PR_OPENED
        assert refreshed.pr_number == 301
        assert refreshed.branch_name is not None

        cli_calls = (
            (
                await session.execute(
                    select(AuditLog).where(
                        AuditLog.action == "cli_invocation", AuditLog.heal_job_id == job.id
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(cli_calls) == 1


async def test_run_heal_job_free_does_not_trust_a_cli_claim_with_no_real_diff(
    isolated_budget_date: None,
    isolated_cli_call_date: None,
    monkeypatch: pytest.MonkeyPatch,
    respx_mock: Any,
    fake_git_remote: str,
) -> None:
    """A CLI transcript that claims success (`is_error=False`, a confident
    final summary) without ever actually applying anything must not be
    treated as a fix — proves the verification is code-enforced, not just a
    read of the CLI's own self-report."""
    monkeypatch.setattr(core_settings, "github_token", "test-token")
    monkeypatch.setattr(core_settings, "github_repo", "acme/repo")
    _mock_github_issue(respx_mock, issue_number=88)

    error_id = await _insert_error(
        exception_type="ZeroDivisionError",
        file_path="apps/target_app/bugs.py",
        line_number=32,
        function_name="average_rating",
    )
    job = await _insert_heal_job(job_type=HealJobType.RUNTIME_ERROR, source_error_id=error_id)

    async def fake_run_claude_cli(prompt: str, *, cwd: Path, **kwargs: Any) -> CLIResult:
        return CLIResult(
            result_text="Fixed it (nothing was actually changed).",
            is_error=False,
            subtype="success",
            num_turns=2,
            session_id="sess-y",
            input_tokens=100,
            output_tokens=50,
            cache_creation_input_tokens=0,
            cache_read_input_tokens=0,
            total_cost_usd=None,
            raw={},
        )

    monkeypatch.setattr(agent_free, "run_claude_cli", fake_run_claude_cli)

    async with connect_in_memory(mcp) as mcp_client, GitHubClient() as github:
        await run_heal_job_free(job.id, mcp=mcp_client, github=github, remote=fake_git_remote)

    async with session_scope() as session:
        refreshed = await session.get(HealJob, job.id)
        assert refreshed is not None
        assert refreshed.status == HealJobStatus.FAILED
        assert refreshed.pr_number is None


async def test_run_heal_job_free_rejects_malicious_diff_from_cli(
    isolated_budget_date: None,
    isolated_cli_call_date: None,
    monkeypatch: pytest.MonkeyPatch,
    respx_mock: Any,
    fake_git_remote: str,
) -> None:
    """Even if the (compromised) model tries to delete a test through the
    CLI's own propose_patch call, the same server-side anti-cheat guardrail
    (mcp_server/patch_guard.py) that protects API mode protects free mode —
    it isn't bypassable by not going through the Anthropic tool-call loop."""
    monkeypatch.setattr(core_settings, "github_token", "test-token")
    monkeypatch.setattr(core_settings, "github_repo", "acme/repo")
    _mock_github_issue(respx_mock, issue_number=99)

    error_id = await _insert_error(
        exception_type="ZeroDivisionError",
        file_path="apps/target_app/bugs.py",
        line_number=32,
        function_name="average_rating",
    )
    job = await _insert_heal_job(job_type=HealJobType.RUNTIME_ERROR, source_error_id=error_id)

    malicious_diff = (
        "--- a/apps/target_app/test_existing_regression_free.py\n"
        "+++ /dev/null\n"
        "@@ -1,2 +0,0 @@\n"
        "-async def test_existing_regression_free() -> None:\n"
        "-    assert True\n"
    )
    rejections: list[str] = []
    mcp_client_ref: dict[str, Any] = {}

    async def fake_run_claude_cli(prompt: str, *, cwd: Path, **kwargs: Any) -> CLIResult:
        from healer.mcp_client import MCPToolError

        try:
            await mcp_client_ref["client"].call_tool(
                "propose_patch",
                {
                    "heal_job_id": job.id,
                    "worktree": f"heal-{job.id}",
                    "unified_diff": malicious_diff,
                },
            )
        except MCPToolError as exc:
            rejections.append(str(exc))
        return CLIResult(
            result_text="Attempted to delete the failing test.",
            is_error=True,
            subtype="error_during_execution",
            num_turns=1,
            session_id="sess-z",
            input_tokens=50,
            output_tokens=20,
            cache_creation_input_tokens=0,
            cache_read_input_tokens=0,
            total_cost_usd=None,
            raw={},
        )

    monkeypatch.setattr(agent_free, "run_claude_cli", fake_run_claude_cli)

    async with connect_in_memory(mcp) as mcp_client, GitHubClient() as github:
        mcp_client_ref["client"] = mcp_client
        await run_heal_job_free(job.id, mcp=mcp_client, github=github, remote=fake_git_remote)

    assert len(rejections) == 3  # once per attempt, MAX_ATTEMPTS
    assert any("delete" in r.lower() or "cheat" in r.lower() for r in rejections)

    async with session_scope() as session:
        refreshed = await session.get(HealJob, job.id)
        assert refreshed is not None
        assert refreshed.status == HealJobStatus.FAILED
        assert refreshed.pr_number is None


async def test_run_heal_job_free_paused_when_cli_call_budget_exhausted(
    isolated_budget_date: None,
    monkeypatch: pytest.MonkeyPatch,
    fake_git_remote: str,
) -> None:
    monkeypatch.setattr(core_settings, "max_cli_calls_per_day", 0)

    error_id = await _insert_error(
        exception_type="ZeroDivisionError",
        file_path="apps/target_app/bugs.py",
        line_number=32,
        function_name="average_rating",
    )
    job = await _insert_heal_job(job_type=HealJobType.RUNTIME_ERROR, source_error_id=error_id)

    called = False

    async def fake_run_claude_cli(*args: Any, **kwargs: Any) -> CLIResult:
        nonlocal called
        called = True
        raise AssertionError("must not be called once the daily CLI-call cap is hit")

    monkeypatch.setattr(agent_free, "run_claude_cli", fake_run_claude_cli)

    async with connect_in_memory(mcp) as mcp_client, GitHubClient() as github:
        await run_heal_job_free(job.id, mcp=mcp_client, github=github, remote=fake_git_remote)

    assert called is False
    async with session_scope() as session:
        refreshed = await session.get(HealJob, job.id)
        assert refreshed is not None
        assert refreshed.status == HealJobStatus.PAUSED_BUDGET

"""healer.agent_codex: the (documented-only, not live-verified) OpenAI Codex
CLI backend.

Mirrors `tests/test_healer_agent_free.py`'s structure exactly: `run_codex_cli`
is tested against a mocked subprocess (never a real `codex` call, per
CLAUDE.md), covering success, timeout, an error event, "not logged in",
"usage limit", CLI-not-found, and malformed (no parseable JSONL) output. The
higher-level loop (`run_heal_job_codex`) is tested end to end against a real
MCP server, real git worktrees and a throwaway git remote, with
`run_codex_cli` itself monkeypatched to a fake that performs its "edits" via
real `git apply`/`mcp.call_tool` calls before returning a scripted
`CLIResult` (same principle as `test_healer_agent_free.py`).
"""

from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import select

from core.config import settings as core_settings
from core.db import session_scope
from core.models import AuditLog, Error, HealJob, HealJobStatus, HealJobType, OpenResolvedStatus
from healer import agent_codex
from healer.agent_codex import (
    CLIMalformedOutputError,
    CLINotFoundError,
    CLINotLoggedInError,
    CLIResult,
    CLITimeoutError,
    CLIUsageLimitError,
    run_codex_cli,
    run_heal_job_codex,
)
from healer.mcp_client import connect_in_memory
from mcp_server.github_client import GITHUB_API_BASE, GitHubClient
from mcp_server.sandbox import REPO_ROOT
from mcp_server.server import mcp


@pytest.fixture(autouse=True)
def _deterministic_cli_resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force the non-Windows-shim `create_subprocess_exec` path by default,
    same rationale as `test_healer_agent_free.py`'s equivalent fixture."""
    monkeypatch.setattr("healer.agent_codex.shutil.which", lambda _name: None)


@dataclass
class _FakeProcess:
    stdout_bytes: bytes = b""
    stderr_bytes: bytes = b""
    returncode: int = 0
    pid: int = 5252
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
    process: _FakeProcess | None
    exc: Exception | None = None
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def __call__(self, *args: Any, **kwargs: Any) -> _FakeProcess:
        self.calls.append({"args": args, "kwargs": kwargs})
        if self.exc is not None:
            raise self.exc
        assert self.process is not None
        return self.process


def _jsonl(*events: dict[str, Any]) -> bytes:
    return "\n".join(json.dumps(e) for e in events).encode("utf-8")


def _success_stdout(*, result: str = "Fixed it.", num_turns: int = 4) -> bytes:
    events = [
        {"type": "turn_start"},
        {"type": "turn_complete"},
        {
            "type": "task_complete",
            "message": result,
            "is_error": False,
            "session_id": "sess-codex-1",
            "usage": {
                "input_tokens": 900,
                "output_tokens": 150,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
            },
        },
    ] * 1
    # Repeat turn markers num_turns times so num_turns is observable.
    turns = [{"type": "turn_complete"} for _ in range(num_turns)]
    return _jsonl(*turns, events[-1])


async def test_run_codex_cli_success_parses_result_and_usage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeExec(process=_FakeProcess(stdout_bytes=_success_stdout()))
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake)

    result = await run_codex_cli("do the thing", cwd=REPO_ROOT, max_turns=5, timeout_s=30)

    assert isinstance(result, CLIResult)
    assert result.result_text == "Fixed it."
    assert result.is_error is False
    assert result.num_turns == 4
    assert result.input_tokens == 900
    assert result.session_id == "sess-codex-1"

    assert len(fake.calls) == 1
    argv = list(fake.calls[0]["args"])
    assert argv[0] == "codex"
    assert argv[1:4] == ["exec", "-", "--json"]
    assert "-c" in argv

    kwargs = fake.calls[0]["kwargs"]
    assert kwargs["cwd"] == str(REPO_ROOT)
    # Prompt goes via stdin, never argv.
    assert "do the thing" not in argv
    assert fake.process is not None
    assert fake.process.sent_stdin == b"do the thing"

    # ANTHROPIC_*/GOOGLE_*/GEMINI_* vars never reach the codex subprocess.
    env = kwargs["env"]
    for key in (
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_MODEL",
        "GOOGLE_API_KEY",
        "GEMINI_API_KEY",
    ):
        assert key not in env
    # A dedicated CODEX_HOME (restricted MCP config) is set.
    assert "CODEX_HOME" in env


async def test_run_codex_cli_writes_selfheal_only_mcp_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The generated CODEX_HOME's config.toml should define exactly the
    selfheal MCP server, pointed at mcp-pod's HTTP endpoint, with a
    read-only sandbox (see module docstring for why).

    `CODEX_HOME` is a `tempfile.TemporaryDirectory()` that's cleaned up as
    soon as `run_codex_cli` returns (correct: the real `codex` subprocess
    only needs it to exist while it's running, inside that `with` block) —
    so this fake reads `config.toml` synchronously from inside its own
    `__call__`, at the moment the subprocess would be spawned, before that
    cleanup happens.
    """
    captured: dict[str, str] = {}

    @dataclass
    class _CapturingFakeExec(_FakeExec):
        async def __call__(self, *args: Any, **kwargs: Any) -> _FakeProcess:
            codex_home = Path(kwargs["env"]["CODEX_HOME"])
            captured["config_text"] = (codex_home / "config.toml").read_text(encoding="utf-8")
            return await super().__call__(*args, **kwargs)

    fake = _CapturingFakeExec(process=_FakeProcess(stdout_bytes=_success_stdout()))
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake)

    await run_codex_cli("prompt", cwd=REPO_ROOT)

    config_text = captured["config_text"]
    assert "[mcp_servers.selfheal]" in config_text
    assert f"http://127.0.0.1:{core_settings.mcp_port}/mcp" in config_text
    assert 'sandbox_mode = "read-only"' in config_text
    assert 'approval_policy = "never"' in config_text


async def test_run_codex_cli_strips_env_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-not-leak")
    monkeypatch.setenv("GEMINI_API_KEY", "should-not-leak-either")
    fake = _FakeExec(process=_FakeProcess(stdout_bytes=_success_stdout()))
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake)

    await run_codex_cli("prompt", cwd=REPO_ROOT)

    env = fake.calls[0]["kwargs"]["env"]
    assert "ANTHROPIC_API_KEY" not in env
    assert "GEMINI_API_KEY" not in env


async def test_run_codex_cli_error_event_still_parses(monkeypatch: pytest.MonkeyPatch) -> None:
    stdout = _jsonl(
        {"type": "turn_complete"},
        {"type": "error", "message": "Could not find a fix.", "is_error": True},
    )
    fake = _FakeExec(process=_FakeProcess(stdout_bytes=stdout))
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake)

    result = await run_codex_cli("prompt", cwd=REPO_ROOT)
    assert result.is_error is True
    assert result.result_text == "Could not find a fix."


async def test_run_codex_cli_timeout_kills_process_tree(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeExec(process=_FakeProcess(hang=True))
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake)

    kill_calls: list[int] = []

    async def fake_kill_tree(pid: int) -> None:
        kill_calls.append(pid)

    monkeypatch.setattr(agent_codex, "kill_process_tree", fake_kill_tree)

    with pytest.raises(CLITimeoutError):
        await run_codex_cli("prompt", cwd=REPO_ROOT, timeout_s=0.01)

    assert kill_calls == [fake.process.pid]  # type: ignore[union-attr]


async def test_run_codex_cli_not_found_raises_clean_error(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeExec(process=None, exc=FileNotFoundError("no such file"))
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake)

    with pytest.raises(CLINotFoundError):
        await run_codex_cli("prompt", cwd=REPO_ROOT)


async def test_run_codex_cli_not_logged_in_detected_from_stderr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeExec(
        process=_FakeProcess(
            stdout_bytes=b"",
            stderr_bytes=b"Error: not authenticated. Run `codex login` first.",
            returncode=1,
        )
    )
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake)

    with pytest.raises(CLINotLoggedInError):
        await run_codex_cli("prompt", cwd=REPO_ROOT)


async def test_run_codex_cli_usage_limit_detected_from_stderr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeExec(
        process=_FakeProcess(
            stdout_bytes=b"",
            stderr_bytes=b"Usage limit reached. Quota exceeded, resets at 5pm.",
            returncode=1,
        )
    )
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake)

    with pytest.raises(CLIUsageLimitError):
        await run_codex_cli("prompt", cwd=REPO_ROOT)


async def test_run_codex_cli_no_parseable_jsonl_raises_clean_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeExec(process=_FakeProcess(stdout_bytes=b"not json at all {{{", returncode=1))
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake)

    with pytest.raises(CLIMalformedOutputError):
        await run_codex_cli("prompt", cwd=REPO_ROOT)


# --- Higher-level loop: run_codex_cli monkeypatched, everything else real --


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


_TEST_FILE = "apps/target_app/test_average_rating_zero_regression_codex.py"
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


async def test_run_heal_job_codex_fixes_zero_division_error_end_to_end(
    isolated_budget_date: None,
    isolated_cli_call_date: None,
    monkeypatch: pytest.MonkeyPatch,
    respx_mock: Any,
    fake_git_remote: str,
) -> None:
    monkeypatch.setattr(core_settings, "github_token", "test-token")
    monkeypatch.setattr(core_settings, "github_repo", "acme/repo")
    _mock_github_pr_and_labels(respx_mock, pr_number=401)
    _mock_github_issue(respx_mock, issue_number=1)

    error_id = await _insert_error(
        exception_type="ZeroDivisionError",
        file_path="apps/target_app/bugs.py",
        line_number=32,
        function_name="average_rating",
    )
    job = await _insert_heal_job(job_type=HealJobType.RUNTIME_ERROR, source_error_id=error_id)

    async def fake_run_codex_cli(prompt: str, *, cwd: Path, **kwargs: Any) -> CLIResult:
        from mcp_server import git_utils

        await git_utils.apply_diff(_new_file_diff(_TEST_FILE, _TEST_FILE_CONTENT), cwd=cwd)
        await git_utils.apply_diff(_fix_diff(), cwd=cwd)
        return CLIResult(
            result_text="Root cause: missing zero guard. Fixed.",
            is_error=False,
            subtype="task_complete",
            num_turns=6,
            session_id="sess-codex-x",
            input_tokens=1200,
            output_tokens=300,
            cache_creation_input_tokens=0,
            cache_read_input_tokens=0,
            total_cost_usd=None,
            raw={},
        )

    monkeypatch.setattr(agent_codex, "run_codex_cli", fake_run_codex_cli)

    async with connect_in_memory(mcp) as mcp_client, GitHubClient() as github:
        await run_heal_job_codex(job.id, mcp=mcp_client, github=github, remote=fake_git_remote)

    async with session_scope() as session:
        refreshed = await session.get(HealJob, job.id)
        assert refreshed is not None
        assert refreshed.status == HealJobStatus.PR_OPENED
        assert refreshed.pr_number == 401
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
        assert cli_calls[0].details["backend"] == "codex_cli"


async def test_run_heal_job_codex_does_not_trust_a_claim_with_no_real_diff(
    isolated_budget_date: None,
    isolated_cli_call_date: None,
    monkeypatch: pytest.MonkeyPatch,
    respx_mock: Any,
    fake_git_remote: str,
) -> None:
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

    async def fake_run_codex_cli(prompt: str, *, cwd: Path, **kwargs: Any) -> CLIResult:
        return CLIResult(
            result_text="Fixed it (nothing was actually changed).",
            is_error=False,
            subtype="task_complete",
            num_turns=2,
            session_id="sess-codex-y",
            input_tokens=100,
            output_tokens=50,
            cache_creation_input_tokens=0,
            cache_read_input_tokens=0,
            total_cost_usd=None,
            raw={},
        )

    monkeypatch.setattr(agent_codex, "run_codex_cli", fake_run_codex_cli)

    async with connect_in_memory(mcp) as mcp_client, GitHubClient() as github:
        await run_heal_job_codex(job.id, mcp=mcp_client, github=github, remote=fake_git_remote)

    async with session_scope() as session:
        refreshed = await session.get(HealJob, job.id)
        assert refreshed is not None
        assert refreshed.status == HealJobStatus.FAILED
        assert refreshed.pr_number is None


async def test_run_heal_job_codex_paused_when_cli_call_budget_exhausted(
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

    async def fake_run_codex_cli(*args: Any, **kwargs: Any) -> CLIResult:
        nonlocal called
        called = True
        raise AssertionError("must not be called once the daily CLI-call cap is hit")

    monkeypatch.setattr(agent_codex, "run_codex_cli", fake_run_codex_cli)

    async with connect_in_memory(mcp) as mcp_client, GitHubClient() as github:
        await run_heal_job_codex(job.id, mcp=mcp_client, github=github, remote=fake_git_remote)

    assert called is False
    async with session_scope() as session:
        refreshed = await session.get(HealJob, job.id)
        assert refreshed is not None
        assert refreshed.status == HealJobStatus.PAUSED_BUDGET

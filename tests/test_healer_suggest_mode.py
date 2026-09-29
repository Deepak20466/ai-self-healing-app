"""Tests for healer/suggest_mode.py: `selfheal fix ... --suggest`'s
no-verification-at-all fix path. Same "fake performs its edits via real git
apply before returning a scripted CLI result" pattern every other AI
backend's tests already established.
"""

from __future__ import annotations

import asyncio
import shutil
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest

from core.config import settings as core_settings
from core.db import session_scope
from core.models import Error, HealJob, HealJobStatus, HealJobType, MonitoredApp, OpenResolvedStatus
from healer import agent_free, suggest_mode
from healer.agent_free import CLIResult
from healer.mcp_client import connect_in_memory
from mcp_server import git_utils
from mcp_server.github_client import GITHUB_API_BASE, GitHubClient
from mcp_server.sandbox import REPO_ROOT
from mcp_server.server import mcp


async def _git(*args: str, cwd: Path) -> None:
    process = await asyncio.create_subprocess_exec(
        "git", *args, cwd=str(cwd), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    _, stderr = await process.communicate()
    assert process.returncode == 0, stderr.decode(errors="replace")


async def _make_connected_app() -> tuple[MonitoredApp, Path]:
    app_name = f"suggest-{uuid.uuid4().hex[:10]}"
    clone_root = REPO_ROOT / "connected_apps" / app_name
    clone_root.mkdir(parents=True)
    (clone_root / "app.py").write_text("VALUE = 1\n")
    await _git("init", "-b", "main", cwd=clone_root)
    await _git("config", "user.email", "test@example.com", cwd=clone_root)
    await _git("config", "user.name", "Test", cwd=clone_root)
    await _git("add", "-A", cwd=clone_root)
    await _git("commit", "-m", "initial", cwd=clone_root)

    async with session_scope() as session:
        app = MonitoredApp(
            name=app_name,
            language="python",
            local_repo_path=f"connected_apps/{app_name}",
            github_repo="acme/suggest",
            allowed_write_paths=[f"connected_apps/{app_name}/"],
            test_command="pytest",
            ingest_token=uuid.uuid4().hex,
            repo_url="https://github.com/acme/suggest",
        )
        session.add(app)
        await session.flush()
        await session.refresh(app)
    return app, clone_root


async def _make_job_for_app(app: MonitoredApp) -> int:
    async with session_scope() as session:
        error = Error(
            fingerprint=f"test-{uuid.uuid4().hex}",
            exception_type="ValueError",
            message="ValueError for test",
            traceback="Traceback (most recent call last):\n  ...",
            file_path="app.py",
            line_number=1,
            function_name="main",
            status=OpenResolvedStatus.OPEN,
            app_id=app.id,
        )
        session.add(error)
        await session.flush()
        job = HealJob(
            type=HealJobType.RUNTIME_ERROR,
            status=HealJobStatus.RUNNING,
            fingerprint=f"test-{uuid.uuid4().hex}",
            source_error_id=error.id,
            app_id=app.id,
        )
        session.add(job)
        await session.flush()
        return job.id


async def _cleanup(app_id: int, clone_root: Path) -> None:
    async with session_scope() as session:
        row = await session.get(MonitoredApp, app_id)
        if row is not None:
            await session.delete(row)
    shutil.rmtree(clone_root, ignore_errors=True)


async def test_run_heal_job_suggest_opens_a_labeled_pr_with_no_verification(
    isolated_cli_call_date: None,
    monkeypatch: pytest.MonkeyPatch,
    respx_mock: Any,
) -> None:
    app, clone_root = await _make_connected_app()
    bare_remote = REPO_ROOT / "connected_apps" / f".bare-{uuid.uuid4().hex[:10]}.git"
    process = await asyncio.create_subprocess_exec(
        "git",
        "init",
        "--bare",
        "-b",
        "main",
        str(bare_remote),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await process.communicate()
    assert process.returncode == 0, stderr.decode(errors="replace")
    try:
        real_commit_and_push = suggest_mode.commit_and_push

        async def _redirect_push(path: Path, branch: str, *, message: str, remote: str) -> None:
            del remote
            await real_commit_and_push(path, branch, message=message, remote=str(bare_remote))

        monkeypatch.setattr(suggest_mode, "commit_and_push", _redirect_push)
        monkeypatch.setattr(core_settings, "github_token", "test-token")
        respx_mock.get(f"{GITHUB_API_BASE}/repos/acme/suggest").mock(
            return_value=httpx.Response(200, json={"default_branch": "main"})
        )
        respx_mock.post(f"{GITHUB_API_BASE}/repos/acme/suggest/pulls").mock(
            return_value=httpx.Response(
                201, json={"number": 701, "html_url": "https://example/pr/701"}
            )
        )
        respx_mock.post(f"{GITHUB_API_BASE}/repos/acme/suggest/issues/701/labels").mock(
            return_value=httpx.Response(200, json=[])
        )

        async def fake_run_claude_cli(prompt: str, *, cwd: Path, **kwargs: Any) -> CLIResult:
            diff = "--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-VALUE = 1\n+VALUE = 2\n"
            await git_utils.apply_diff(diff, cwd=cwd)
            return CLIResult(
                result_text="Best guess: wrong constant.",
                is_error=False,
                subtype="success",
                num_turns=2,
                session_id="sess-suggest",
                input_tokens=100,
                output_tokens=20,
                cache_creation_input_tokens=0,
                cache_read_input_tokens=0,
                total_cost_usd=None,
                raw={},
            )

        monkeypatch.setattr(agent_free, "run_claude_cli", fake_run_claude_cli)

        job_id = await _make_job_for_app(app)
        async with (
            connect_in_memory(mcp) as mcp_client,
            GitHubClient(repo="acme/suggest") as github,
        ):
            await suggest_mode.run_heal_job_suggest(job_id, mcp=mcp_client, github=github)

        async with session_scope() as session:
            refreshed = await session.get(HealJob, job_id)
            assert refreshed is not None
            assert refreshed.status == HealJobStatus.PR_OPENED
            assert refreshed.pr_number == 701
            assert refreshed.auto_merge_override is False

        labels_request = respx_mock.calls[-1].request
        assert b"unverified-suggestion" in labels_request.content
    finally:
        shutil.rmtree(bare_remote, ignore_errors=True)
        await _cleanup(app.id, clone_root)


async def test_run_heal_job_suggest_no_diff_opens_an_issue_not_a_pr(
    isolated_cli_call_date: None,
    monkeypatch: pytest.MonkeyPatch,
    respx_mock: Any,
) -> None:
    app, clone_root = await _make_connected_app()
    try:
        monkeypatch.setattr(core_settings, "github_token", "test-token")
        respx_mock.post(f"{GITHUB_API_BASE}/repos/acme/suggest/issues").mock(
            return_value=httpx.Response(201, json={"number": 55})
        )

        async def fake_run_claude_cli(prompt: str, *, cwd: Path, **kwargs: Any) -> CLIResult:
            return CLIResult(
                result_text="I could not find anything to fix.",
                is_error=False,
                subtype="success",
                num_turns=1,
                session_id="sess-no-diff",
                input_tokens=50,
                output_tokens=10,
                cache_creation_input_tokens=0,
                cache_read_input_tokens=0,
                total_cost_usd=None,
                raw={},
            )

        monkeypatch.setattr(agent_free, "run_claude_cli", fake_run_claude_cli)

        job_id = await _make_job_for_app(app)
        async with (
            connect_in_memory(mcp) as mcp_client,
            GitHubClient(repo="acme/suggest") as github,
        ):
            await suggest_mode.run_heal_job_suggest(job_id, mcp=mcp_client, github=github)

        async with session_scope() as session:
            refreshed = await session.get(HealJob, job_id)
            assert refreshed is not None
            assert refreshed.status == HealJobStatus.FAILED
            assert refreshed.pr_number is None
    finally:
        await _cleanup(app.id, clone_root)

"""Tests for healer/onboarding_prepare.py: the AI-driven onboarding PR
(starter tests + CI workflow + test config) offered by `selfheal prepare
--onboard`. Same "fake performs its edits via real git apply before
returning a scripted CLI result" pattern every other AI backend's tests
already established -- GitHub is mocked via respx, the CLI is a hand-built
fake, everything else (worktree, git apply, run_tests through a real MCP
round trip) is real.
"""

from __future__ import annotations

import asyncio
import shutil
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import select

from core.config import settings as core_settings
from core.db import session_scope
from core.models import HealJob, HealJobStatus, MonitoredApp
from healer import agent_free, onboarding_prepare
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


async def _make_connected_app(*, with_tests: bool) -> tuple[MonitoredApp, Path]:
    app_name = f"onboard-{uuid.uuid4().hex[:10]}"
    clone_root = REPO_ROOT / "connected_apps" / app_name
    clone_root.mkdir(parents=True)
    (clone_root / "requirements.txt").write_text("fastapi\n")
    (clone_root / "app.py").write_text("VALUE = 1\n")
    if with_tests:
        (clone_root / "test_app.py").write_text("def test_x(): assert True\n")
        (clone_root / ".github" / "workflows").mkdir(parents=True)
        (clone_root / ".github" / "workflows" / "ci.yml").write_text(
            "name: CI\njobs:\n  test:\n    steps:\n      - run: pytest\n"
        )

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
            github_repo="acme/onboard",
            allowed_write_paths=[f"connected_apps/{app_name}/"],
            test_command="pytest",
            ingest_token=uuid.uuid4().hex,
            repo_url="https://github.com/acme/onboard",
        )
        session.add(app)
        await session.flush()
        await session.refresh(app)
    return app, clone_root


async def _cleanup(app_id: int, clone_root: Path) -> None:
    async with session_scope() as session:
        row = await session.get(MonitoredApp, app_id)
        if row is not None:
            await session.delete(row)
    shutil.rmtree(clone_root, ignore_errors=True)


def test_estimate_onboarding_cost_scales_with_scope() -> None:
    from core.repo_health_check import PrepareReport

    small = PrepareReport(
        app_name="x",
        language="python",
        has_tests=True,
        test_file_count=1,
        has_ci_workflow=False,
        ci_runs_tests=False,
        external_services=(),
        heavy_dependencies=(),
        missing=("a",),
    )
    large = PrepareReport(
        app_name="x",
        language="python",
        has_tests=False,
        test_file_count=0,
        has_ci_workflow=False,
        ci_runs_tests=False,
        external_services=("postgresql",),
        heavy_dependencies=(),
        missing=("a", "b", "c", "d"),
    )
    assert "small" in onboarding_prepare.estimate_onboarding_cost(small)
    assert "larger" in onboarding_prepare.estimate_onboarding_cost(large)


async def test_run_onboarding_prepare_skips_when_already_fixable(
    isolated_cli_call_date: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, clone_root = await _make_connected_app(with_tests=True)
    try:

        async def fail_if_called(*args: Any, **kwargs: Any) -> CLIResult:
            raise AssertionError("must not call the CLI when nothing is missing")

        monkeypatch.setattr(agent_free, "run_claude_cli", fail_if_called)

        async with (
            connect_in_memory(mcp) as mcp_client,
            GitHubClient(repo="acme/onboard") as github,
        ):
            result = await onboarding_prepare.run_onboarding_prepare(
                app.id, mcp=mcp_client, github=github
            )

        assert result.status == "already_fixable"
    finally:
        await _cleanup(app.id, clone_root)


async def test_run_onboarding_prepare_opens_a_labeled_pr_after_local_verification(
    isolated_cli_call_date: None,
    monkeypatch: pytest.MonkeyPatch,
    respx_mock: Any,
) -> None:
    app, clone_root = await _make_connected_app(with_tests=False)
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
        real_commit_and_push = onboarding_prepare.commit_and_push

        async def _redirect_push(path: Path, branch: str, *, message: str, remote: str) -> None:
            del remote
            await real_commit_and_push(path, branch, message=message, remote=str(bare_remote))

        monkeypatch.setattr(onboarding_prepare, "commit_and_push", _redirect_push)
        monkeypatch.setattr(core_settings, "github_token", "test-token")
        respx_mock.get(f"{GITHUB_API_BASE}/repos/acme/onboard").mock(
            return_value=httpx.Response(200, json={"default_branch": "main"})
        )
        respx_mock.post(f"{GITHUB_API_BASE}/repos/acme/onboard/pulls").mock(
            return_value=httpx.Response(
                201, json={"number": 901, "html_url": "https://example/pr/901"}
            )
        )
        respx_mock.post(f"{GITHUB_API_BASE}/repos/acme/onboard/issues/901/labels").mock(
            return_value=httpx.Response(200, json=[])
        )

        async def fake_run_claude_cli(prompt: str, *, cwd: Path, **kwargs: Any) -> CLIResult:
            diff = (
                "--- /dev/null\n+++ b/test_app.py\n"
                "@@ -0,0 +1,2 @@\n"
                "+def test_x():\n"
                "+    assert True\n"
            )
            await git_utils.apply_diff(diff, cwd=cwd)
            return CLIResult(
                result_text="Added one starter test.",
                is_error=False,
                subtype="success",
                num_turns=2,
                session_id="sess-onboard",
                input_tokens=200,
                output_tokens=50,
                cache_creation_input_tokens=0,
                cache_read_input_tokens=0,
                total_cost_usd=None,
                raw={},
            )

        monkeypatch.setattr(agent_free, "run_claude_cli", fake_run_claude_cli)

        async with (
            connect_in_memory(mcp) as mcp_client,
            GitHubClient(repo="acme/onboard") as github,
        ):
            result = await onboarding_prepare.run_onboarding_prepare(
                app.id, mcp=mcp_client, github=github
            )

        assert result.status == "pr_opened", result.detail
        assert result.pr_number == 901

        labels_request = respx_mock.calls[-1].request
        assert b"selfheal-onboarding" in labels_request.content
        assert b"verified-by-ci-not-locally" not in labels_request.content

        async with session_scope() as session:
            jobs = (
                (await session.execute(select(HealJob).where(HealJob.app_id == app.id)))
                .scalars()
                .all()
            )
            assert len(jobs) == 1
            assert jobs[0].status == HealJobStatus.PR_OPENED
            assert jobs[0].auto_merge_override is False
    finally:
        shutil.rmtree(bare_remote, ignore_errors=True)
        await _cleanup(app.id, clone_root)


async def test_run_onboarding_prepare_no_pr_when_generated_tests_fail_locally(
    isolated_cli_call_date: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, clone_root = await _make_connected_app(with_tests=False)
    try:

        async def fake_run_claude_cli(prompt: str, *, cwd: Path, **kwargs: Any) -> CLIResult:
            diff = (
                "--- /dev/null\n+++ b/test_app.py\n"
                "@@ -0,0 +1,2 @@\n"
                "+def test_x():\n"
                "+    assert False\n"
            )
            await git_utils.apply_diff(diff, cwd=cwd)
            return CLIResult(
                result_text="Added one starter test.",
                is_error=False,
                subtype="success",
                num_turns=2,
                session_id="sess-fail",
                input_tokens=200,
                output_tokens=50,
                cache_creation_input_tokens=0,
                cache_read_input_tokens=0,
                total_cost_usd=None,
                raw={},
            )

        monkeypatch.setattr(agent_free, "run_claude_cli", fake_run_claude_cli)

        async with (
            connect_in_memory(mcp) as mcp_client,
            GitHubClient(repo="acme/onboard") as github,
        ):
            result = await onboarding_prepare.run_onboarding_prepare(
                app.id, mcp=mcp_client, github=github
            )

        assert result.status == "failed"
        assert result.pr_number is None
        async with session_scope() as session:
            jobs = (
                (await session.execute(select(HealJob).where(HealJob.app_id == app.id)))
                .scalars()
                .all()
            )
            assert len(jobs) == 1
            assert jobs[0].status == HealJobStatus.FAILED
    finally:
        await _cleanup(app.id, clone_root)

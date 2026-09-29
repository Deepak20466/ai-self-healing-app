"""Tests for healer/remote_verify.py: the CI-verified fix path for a
connected app whose manifest names a heavy/ML dependency this project never
installs locally (see core/scanner.py:detect_heavy_dependencies).

`has_ci_workflow` is a pure filesystem check, tested directly. The
end-to-end test is the same "fake performs its edits via real git apply
before returning a scripted CLI result, so the code's own post-hoc checks
are what's actually being tested" pattern tests/test_healer_agent_free.py
already established -- no local test run is expected here (that's the
whole point of this mode), so what's actually verified is: no CI workflow
-> skipped, a real diff -> a labeled PR opened with auto_merge_override
forced False, and healer/worker.py routes a heavy-dependency job here
instead of through the normal AI_CHAIN dispatch.
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
from core.models import (
    AuditLog,
    Error,
    HealJob,
    HealJobStatus,
    HealJobType,
    MonitoredApp,
    OpenResolvedStatus,
)
from healer import agent_free, remote_verify
from healer.agent_free import CLIResult
from healer.mcp_client import connect_in_memory
from mcp_server import git_utils
from mcp_server.github_client import GITHUB_API_BASE, GitHubClient
from mcp_server.sandbox import REPO_ROOT
from mcp_server.server import mcp


def test_has_ci_workflow_true_when_a_yml_file_is_present(tmp_path: Path) -> None:
    (tmp_path / ".github" / "workflows").mkdir(parents=True)
    (tmp_path / ".github" / "workflows" / "ci.yml").write_text("name: CI\n")
    assert remote_verify.has_ci_workflow(tmp_path) is True


def test_has_ci_workflow_false_when_the_directory_is_missing(tmp_path: Path) -> None:
    assert remote_verify.has_ci_workflow(tmp_path) is False


def test_has_ci_workflow_false_when_the_directory_is_empty(tmp_path: Path) -> None:
    (tmp_path / ".github" / "workflows").mkdir(parents=True)
    assert remote_verify.has_ci_workflow(tmp_path) is False


async def _git(*args: str, cwd: Path) -> None:
    process = await asyncio.create_subprocess_exec(
        "git", *args, cwd=str(cwd), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    _, stderr = await process.communicate()
    assert process.returncode == 0, stderr.decode(errors="replace")


async def _make_connected_app(
    *, with_ci_workflow: bool, heavy_dependency: bool
) -> tuple[MonitoredApp, Path]:
    app_name = f"heavy-remote-{uuid.uuid4().hex[:10]}"
    clone_root = REPO_ROOT / "connected_apps" / app_name
    if with_ci_workflow:
        (clone_root / ".github" / "workflows").mkdir(parents=True)
        (clone_root / ".github" / "workflows" / "ci.yml").write_text(
            "name: CI\non: [push]\njobs: {}\n"
        )
    else:
        clone_root.mkdir(parents=True)
    requirements = "torch>=2.0\nfastapi\n" if heavy_dependency else "fastapi\n"
    (clone_root / "requirements.txt").write_text(requirements)
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
            github_repo="acme/heavy",
            allowed_write_paths=[f"connected_apps/{app_name}/"],
            test_command="pytest",
            ingest_token=uuid.uuid4().hex,
            repo_url="https://github.com/acme/heavy",
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


async def test_run_heal_job_remote_verify_skips_when_no_ci_workflow(
    isolated_cli_call_date: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, clone_root = await _make_connected_app(with_ci_workflow=False, heavy_dependency=True)
    try:
        job_id = await _make_job_for_app(app)

        async def fail_if_called(*args: Any, **kwargs: Any) -> CLIResult:
            raise AssertionError("must not call the CLI when there's no CI to verify against")

        monkeypatch.setattr(agent_free, "run_claude_cli", fail_if_called)
        async with connect_in_memory(mcp) as mcp_client, GitHubClient(repo="acme/heavy") as github:
            await remote_verify.run_heal_job_remote_verify(job_id, mcp=mcp_client, github=github)

        async with session_scope() as session:
            refreshed = await session.get(HealJob, job_id)
            assert refreshed is not None
            assert refreshed.status == HealJobStatus.FAILED
            assert refreshed.error_message is not None
            assert "no GitHub Actions workflow" in refreshed.error_message
    finally:
        await _cleanup(app.id, clone_root)


async def test_run_heal_job_remote_verify_opens_a_labeled_pr_never_auto_merged(
    isolated_cli_call_date: None,
    monkeypatch: pytest.MonkeyPatch,
    respx_mock: Any,
) -> None:
    """`app.github_repo`'s real push URL is never reachable in a test, so
    `commit_and_push` is monkeypatched to push to a throwaway local bare
    repo instead -- same "the push destination is swapped, not the push
    logic itself" principle `fake_git_remote` uses elsewhere, just via a
    real filesystem path since a connected-app worktree is its own git
    clone with no `origin`-style remote already registered for a bare
    remote *name* to resolve against (unlike `create_worktree`'s worktrees,
    which share this project's own `.git` config)."""
    app, clone_root = await _make_connected_app(with_ci_workflow=True, heavy_dependency=True)
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
        job_id = await _make_job_for_app(app)

        real_commit_and_push = remote_verify.commit_and_push

        async def _redirect_push(path: Path, branch: str, *, message: str, remote: str) -> None:
            del remote
            await real_commit_and_push(path, branch, message=message, remote=str(bare_remote))

        monkeypatch.setattr(remote_verify, "commit_and_push", _redirect_push)
        monkeypatch.setattr(core_settings, "github_token", "test-token")
        respx_mock.get(f"{GITHUB_API_BASE}/repos/acme/heavy").mock(
            return_value=httpx.Response(
                200, json={"default_branch": "main", "permissions": {"push": True}}
            )
        )
        respx_mock.post(f"{GITHUB_API_BASE}/repos/acme/heavy/pulls").mock(
            return_value=httpx.Response(
                201, json={"number": 501, "html_url": "https://example/pr/501"}
            )
        )
        respx_mock.post(f"{GITHUB_API_BASE}/repos/acme/heavy/issues/501/labels").mock(
            return_value=httpx.Response(200, json=[])
        )

        async def fake_run_claude_cli(prompt: str, *, cwd: Path, **kwargs: Any) -> CLIResult:
            diff = "--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-VALUE = 1\n+VALUE = 2\n"
            await git_utils.apply_diff(diff, cwd=cwd)
            return CLIResult(
                result_text="Root cause: wrong constant. Fixed.",
                is_error=False,
                subtype="success",
                num_turns=3,
                session_id="sess-remote-verify",
                input_tokens=500,
                output_tokens=100,
                cache_creation_input_tokens=0,
                cache_read_input_tokens=0,
                total_cost_usd=None,
                raw={},
            )

        monkeypatch.setattr(agent_free, "run_claude_cli", fake_run_claude_cli)

        async with connect_in_memory(mcp) as mcp_client, GitHubClient(repo="acme/heavy") as github:
            await remote_verify.run_heal_job_remote_verify(job_id, mcp=mcp_client, github=github)

        async with session_scope() as session:
            refreshed = await session.get(HealJob, job_id)
            assert refreshed is not None
            assert refreshed.status == HealJobStatus.PR_OPENED
            assert refreshed.pr_number == 501
            assert refreshed.branch_name is not None
            assert refreshed.auto_merge_override is False

            labels_request = respx_mock.calls[-1].request
            assert b"verified-by-ci-not-locally" in labels_request.content

            audit_rows = (
                (
                    await session.execute(
                        select(AuditLog).where(
                            AuditLog.action == "pr_opened", AuditLog.heal_job_id == job_id
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert len(audit_rows) == 1
            assert audit_rows[0].details is not None
            assert audit_rows[0].details.get("remote_verify") is True
    finally:
        shutil.rmtree(bare_remote, ignore_errors=True)
        await _cleanup(app.id, clone_root)


async def test_run_heal_job_remote_verify_no_diff_opens_an_issue_not_a_pr(
    isolated_cli_call_date: None,
    monkeypatch: pytest.MonkeyPatch,
    respx_mock: Any,
) -> None:
    app, clone_root = await _make_connected_app(with_ci_workflow=True, heavy_dependency=True)
    try:
        job_id = await _make_job_for_app(app)
        monkeypatch.setattr(core_settings, "github_token", "test-token")
        respx_mock.post(f"{GITHUB_API_BASE}/repos/acme/heavy/issues").mock(
            return_value=httpx.Response(201, json={"number": 77})
        )

        async def fake_run_claude_cli(prompt: str, *, cwd: Path, **kwargs: Any) -> CLIResult:
            return CLIResult(
                result_text="I could not find the bug.",
                is_error=False,
                subtype="success",
                num_turns=1,
                session_id="sess-no-diff",
                input_tokens=100,
                output_tokens=20,
                cache_creation_input_tokens=0,
                cache_read_input_tokens=0,
                total_cost_usd=None,
                raw={},
            )

        monkeypatch.setattr(agent_free, "run_claude_cli", fake_run_claude_cli)

        async with connect_in_memory(mcp) as mcp_client, GitHubClient(repo="acme/heavy") as github:
            await remote_verify.run_heal_job_remote_verify(job_id, mcp=mcp_client, github=github)

        async with session_scope() as session:
            refreshed = await session.get(HealJob, job_id)
            assert refreshed is not None
            assert refreshed.status == HealJobStatus.FAILED
            assert refreshed.pr_number is None
    finally:
        await _cleanup(app.id, clone_root)

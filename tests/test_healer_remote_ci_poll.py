"""Tests for healer/remote_ci_poll.py: polling a remote-verify PR's real
GitHub check-run status and retrying (once, twice max) on a real CI
failure.

Same "fake performs its edits via real git apply/push before returning a
scripted CLI result, so the code's own post-hoc checks are what's actually
being tested" pattern `tests/test_healer_remote_verify.py`/
`tests/test_healer_agent_free.py` already established -- real git worktrees
(a throwaway local bare repo standing in for the connected app's real
GitHub remote, same as `test_healer_remote_verify.py`'s own
`test_run_heal_job_remote_verify_opens_a_labeled_pr_never_auto_merged`),
GitHub mocked via respx, Claude Code CLI mocked via a monkeypatched
`agent_free.run_claude_cli`.

**Never run against a real PR or real GitHub Actions run** -- see
CLAUDE.md's own "mocked tests only" note for this feature; this file proves
the polling/retry/circuit-breaker LOGIC, not that a real connected repo's
CI actually gets fixed.
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
    HealJob,
    HealJobStatus,
    HealJobType,
    MonitoredApp,
)
from healer import agent_free, remote_ci_poll
from healer.agent_free import CLIResult
from mcp_server import git_utils
from mcp_server.github_client import GITHUB_API_BASE
from mcp_server.sandbox import REPO_ROOT


async def _git(*args: str, cwd: Path) -> None:
    process = await asyncio.create_subprocess_exec(
        "git", *args, cwd=str(cwd), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    _, stderr = await process.communicate()
    assert process.returncode == 0, stderr.decode(errors="replace")


async def _make_connected_app_with_open_pr(
    *, attempt_count: int
) -> tuple[MonitoredApp, int, Path, Path, str]:
    """A connected app, a bare repo standing in for its real GitHub remote,
    a branch already pushed to it (simulating a prior `run_heal_job_remote_verify`
    attempt), and a heal_job row already sitting at PR_OPENED with that
    branch/pr_number, tagged with the same `pr_opened`/`remote_verify: True`
    audit_log row the real function writes -- exactly what
    `_find_awaiting_ci_job_ids` looks for.
    """
    app_name = f"heavy-remote-ci-{uuid.uuid4().hex[:10]}"
    clone_root = REPO_ROOT / "connected_apps" / app_name
    clone_root.mkdir(parents=True)
    (clone_root / "requirements.txt").write_text("torch>=2.0\nfastapi\n")
    (clone_root / "app.py").write_text("VALUE = 1\n")
    await _git("init", "-b", "main", cwd=clone_root)
    await _git("config", "user.email", "test@example.com", cwd=clone_root)
    await _git("config", "user.name", "Test", cwd=clone_root)
    await _git("add", "-A", cwd=clone_root)
    await _git("commit", "-m", "initial", cwd=clone_root)

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

    branch = f"autofix/test-{uuid.uuid4().hex[:8]}"
    fix_clone = REPO_ROOT / "connected_apps" / f".fixclone-{uuid.uuid4().hex[:10]}"
    await _git("clone", str(clone_root), str(fix_clone), cwd=REPO_ROOT)
    await _git("checkout", "-b", branch, cwd=fix_clone)
    await _git("config", "user.email", "test@example.com", cwd=fix_clone)
    await _git("config", "user.name", "Test", cwd=fix_clone)
    (fix_clone / "app.py").write_text("VALUE = 2\n")
    await _git("add", "-A", cwd=fix_clone)
    await _git("commit", "-m", "first attempt", cwd=fix_clone)
    await _git("push", str(bare_remote), branch, cwd=fix_clone)
    shutil.rmtree(fix_clone, ignore_errors=True)

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
        job = HealJob(
            type=HealJobType.RUNTIME_ERROR,
            status=HealJobStatus.PR_OPENED,
            fingerprint=f"test-{uuid.uuid4().hex}",
            app_id=app.id,
            pr_number=501,
            branch_name=branch,
            attempt_count=attempt_count,
            auto_merge_override=False,
        )
        session.add(job)
        await session.flush()
        session.add(
            AuditLog(
                action="pr_opened",
                actor="healer",
                heal_job_id=job.id,
                details={"pr_number": 501, "branch": branch, "remote_verify": True},
            )
        )
        job_id = job.id
        await session.refresh(app)
    return app, job_id, clone_root, bare_remote, branch


async def _cleanup(app_id: int, clone_root: Path, bare_remote: Path) -> None:
    async with session_scope() as session:
        row = await session.get(MonitoredApp, app_id)
        if row is not None:
            await session.delete(row)
    shutil.rmtree(clone_root, ignore_errors=True)
    shutil.rmtree(bare_remote, ignore_errors=True)


class _FakeCheckRunsClient:
    def __init__(self, runs: list[dict[str, Any]]) -> None:
        self._runs = runs

    async def list_check_runs(self, sha: str) -> list[dict[str, Any]]:
        del sha
        return self._runs


async def test_check_run_verdict_pending_when_a_run_is_still_running() -> None:
    client = _FakeCheckRunsClient([{"status": "in_progress", "conclusion": None}])
    verdict = await remote_ci_poll._check_run_verdict(client, "sha")  # type: ignore[arg-type]
    assert verdict == "pending"


async def test_check_run_verdict_success_when_all_completed_and_green() -> None:
    client = _FakeCheckRunsClient(
        [
            {"status": "completed", "conclusion": "success"},
            {"status": "completed", "conclusion": "skipped"},
        ]
    )
    verdict = await remote_ci_poll._check_run_verdict(client, "sha")  # type: ignore[arg-type]
    assert verdict == "success"


async def test_check_run_verdict_failure_when_a_completed_run_failed() -> None:
    client = _FakeCheckRunsClient(
        [
            {"status": "completed", "conclusion": "success"},
            {"status": "completed", "conclusion": "failure"},
        ]
    )
    verdict = await remote_ci_poll._check_run_verdict(client, "sha")  # type: ignore[arg-type]
    assert verdict == "failure"


async def test_check_run_verdict_pending_with_no_runs_at_all() -> None:
    client = _FakeCheckRunsClient([])
    verdict = await remote_ci_poll._check_run_verdict(client, "sha")  # type: ignore[arg-type]
    assert verdict == "pending"


async def test_poll_retries_once_on_a_real_ci_failure_and_pushes_a_new_commit(
    isolated_cli_call_date: None,
    monkeypatch: pytest.MonkeyPatch,
    respx_mock: Any,
) -> None:
    app, job_id, clone_root, bare_remote, branch = await _make_connected_app_with_open_pr(
        attempt_count=1
    )
    try:
        monkeypatch.setattr(core_settings, "github_token", "test-token")

        # Redirect the retry's push/fetch target from the real GitHub URL to
        # the throwaway bare repo, same "swap the destination, not the
        # logic" principle test_healer_remote_verify.py already uses.
        real_create = remote_ci_poll.create_worktree_for_connected_app_branch

        async def _redirect_create(
            name: str, br: str, *, source_dir: Path, github_remote_url: str
        ) -> Path:
            del github_remote_url
            return await real_create(
                name, br, source_dir=source_dir, github_remote_url=str(bare_remote)
            )

        real_push = remote_ci_poll.commit_and_push

        async def _redirect_push(path: Path, br: str, *, message: str, remote: str) -> None:
            del remote
            await real_push(path, br, message=message, remote=str(bare_remote))

        monkeypatch.setattr(
            remote_ci_poll, "create_worktree_for_connected_app_branch", _redirect_create
        )
        monkeypatch.setattr(remote_ci_poll, "commit_and_push", _redirect_push)

        respx_mock.get(f"{GITHUB_API_BASE}/repos/acme/heavy/pulls/501").mock(
            return_value=httpx.Response(
                200,
                json={
                    "merged": False,
                    "state": "open",
                    "head": {"sha": "deadbeef"},
                },
            )
        )
        respx_mock.get(f"{GITHUB_API_BASE}/repos/acme/heavy/commits/deadbeef/check-runs").mock(
            return_value=httpx.Response(
                200,
                json={
                    "check_runs": [
                        {
                            "status": "completed",
                            "conclusion": "failure",
                            "name": "CI",
                            "output": {"summary": "a test failed"},
                        }
                    ]
                },
            )
        )

        async def fake_run_claude_cli(prompt: str, *, cwd: Path, **kwargs: Any) -> CLIResult:
            diff = "--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-VALUE = 2\n+VALUE = 3\n"
            await git_utils.apply_diff(diff, cwd=cwd)
            return CLIResult(
                result_text="Root cause: still wrong constant. Fixed again.",
                is_error=False,
                subtype="success",
                num_turns=2,
                session_id="sess-retry",
                input_tokens=200,
                output_tokens=40,
                cache_creation_input_tokens=0,
                cache_read_input_tokens=0,
                total_cost_usd=None,
                raw={},
            )

        monkeypatch.setattr(agent_free, "run_claude_cli", fake_run_claude_cli)

        # >=1, not ==1: the shared test DB can carry other still-pending
        # remote-verify PR_OPENED jobs from other tests/earlier runs (same
        # "shared DB, unscoped global query" family of gotcha CLAUDE.md's
        # own history documents repeatedly) -- other jobs' own GitHub calls
        # simply aren't mocked here and will raise/log a warning inside
        # `_retry_one`'s try/except, which is fine; this test only asserts
        # on ITS OWN job's outcome below.
        seen = await remote_ci_poll.poll_remote_verify_prs_once()
        assert seen >= 1

        async with session_scope() as session:
            refreshed = await session.get(HealJob, job_id)
            assert refreshed is not None
            assert refreshed.status == HealJobStatus.PR_OPENED
            assert refreshed.attempt_count == 2

            retry_rows = (
                (
                    await session.execute(
                        select(AuditLog).where(
                            AuditLog.action == "remote_verify_ci_retry_pushed",
                            AuditLog.heal_job_id == job_id,
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert len(retry_rows) == 1
    finally:
        await _cleanup(app.id, clone_root, bare_remote)


async def test_poll_leaves_a_still_pending_pr_untouched(
    isolated_cli_call_date: None,
    monkeypatch: pytest.MonkeyPatch,
    respx_mock: Any,
) -> None:
    app, job_id, clone_root, bare_remote, branch = await _make_connected_app_with_open_pr(
        attempt_count=1
    )
    try:

        async def fail_if_called(*args: Any, **kwargs: Any) -> CLIResult:
            raise AssertionError("must not call the CLI while CI is still pending")

        monkeypatch.setattr(agent_free, "run_claude_cli", fail_if_called)

        respx_mock.get(f"{GITHUB_API_BASE}/repos/acme/heavy/pulls/501").mock(
            return_value=httpx.Response(
                200, json={"merged": False, "state": "open", "head": {"sha": "deadbeef"}}
            )
        )
        respx_mock.get(f"{GITHUB_API_BASE}/repos/acme/heavy/commits/deadbeef/check-runs").mock(
            return_value=httpx.Response(
                200, json={"check_runs": [{"status": "in_progress", "conclusion": None}]}
            )
        )

        await remote_ci_poll.poll_remote_verify_prs_once()

        async with session_scope() as session:
            refreshed = await session.get(HealJob, job_id)
            assert refreshed is not None
            assert refreshed.status == HealJobStatus.PR_OPENED
            assert refreshed.attempt_count == 1
    finally:
        await _cleanup(app.id, clone_root, bare_remote)


async def test_poll_gives_up_and_opens_an_issue_once_the_attempt_cap_is_reached(
    isolated_cli_call_date: None,
    monkeypatch: pytest.MonkeyPatch,
    respx_mock: Any,
) -> None:
    app, job_id, clone_root, bare_remote, branch = await _make_connected_app_with_open_pr(
        attempt_count=remote_ci_poll.MAX_REMOTE_VERIFY_CI_ATTEMPTS
    )
    try:

        async def fail_if_called(*args: Any, **kwargs: Any) -> CLIResult:
            raise AssertionError("must not call the CLI once the attempt cap is reached")

        monkeypatch.setattr(agent_free, "run_claude_cli", fail_if_called)
        monkeypatch.setattr(core_settings, "github_token", "test-token")

        respx_mock.get(f"{GITHUB_API_BASE}/repos/acme/heavy/pulls/501").mock(
            return_value=httpx.Response(
                200, json={"merged": False, "state": "open", "head": {"sha": "deadbeef"}}
            )
        )
        respx_mock.get(f"{GITHUB_API_BASE}/repos/acme/heavy/commits/deadbeef/check-runs").mock(
            return_value=httpx.Response(
                200,
                json={
                    "check_runs": [{"status": "completed", "conclusion": "failure", "name": "CI"}]
                },
            )
        )
        respx_mock.post(f"{GITHUB_API_BASE}/repos/acme/heavy/issues").mock(
            return_value=httpx.Response(201, json={"number": 909})
        )

        await remote_ci_poll.poll_remote_verify_prs_once()

        async with session_scope() as session:
            refreshed = await session.get(HealJob, job_id)
            assert refreshed is not None
            assert refreshed.status == HealJobStatus.FAILED
            assert refreshed.error_message is not None
            assert "retry cap reached" in refreshed.error_message

        issue_calls = [c for c in respx_mock.calls if c.request.url.path.endswith("/issues")]
        assert len(issue_calls) == 1
    finally:
        await _cleanup(app.id, clone_root, bare_remote)

"""mcp_server.tools.code: read_file, search_code, list_files, blame, commits,
run_tests, propose_patch — especially propose_patch's scope enforcement.
"""

from __future__ import annotations

import subprocess
import uuid
from pathlib import Path

import pytest

from core.db import session_scope
from core.models import HealJob, HealJobStatus, HealJobType, MonitoredApp
from mcp_server.tools._exceptions import ToolError
from mcp_server.tools.code import (
    get_git_blame,
    get_recent_commits,
    list_files,
    propose_patch,
    read_file,
    run_tests,
    search_code,
)


async def _make_heal_job(job_type: HealJobType, app_id: int | None = None) -> int:
    # Random suffix, not a fixed "fp-{job_type}" - a fixed fingerprint would
    # accumulate one real row per job_type per test run in the shared dev DB.
    fingerprint = f"fp-{job_type}-{uuid.uuid4().hex[:8]}"
    async with session_scope() as session:
        job = HealJob(
            type=job_type, status=HealJobStatus.RUNNING, fingerprint=fingerprint, app_id=app_id
        )
        session.add(job)
        await session.flush()
        job_id = job.id
    return job_id


async def _make_monitored_app(
    *, allowed_write_paths: list[str], test_command: str, language: str = "node"
) -> int:
    async with session_scope() as session:
        app = MonitoredApp(
            name=f"scratch-app-{uuid.uuid4().hex[:8]}",
            language=language,
            local_repo_path="examples/scratch_app",
            github_repo="example/scratch",
            allowed_write_paths=allowed_write_paths,
            test_command=test_command,
            ingest_token=uuid.uuid4().hex,
        )
        session.add(app)
        await session.flush()
        app_id = app.id
    return app_id


def _diff_for(worktree_path: Path, relative_path: str, prepend: str) -> str:
    target = worktree_path / relative_path
    original = target.read_text(encoding="utf-8")
    target.write_text(prepend + original, encoding="utf-8")
    diff_text = subprocess.run(
        ["git", "diff", "--", relative_path],
        cwd=str(worktree_path),
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    subprocess.run(
        ["git", "checkout", "--", relative_path],
        cwd=str(worktree_path),
        check=True,
        capture_output=True,
    )
    return diff_text


# --- read_file / search_code / list_files ------------------------------------


async def test_read_file_returns_requested_line_range() -> None:
    result = await read_file("SPEC.md", start_line=1, end_line=3)
    assert result["start_line"] == 1
    assert result["end_line"] == 3
    assert result["content"]


async def test_read_file_rejects_dotenv() -> None:
    with pytest.raises(ToolError):
        await read_file(".env")


async def test_read_file_rejects_start_after_end() -> None:
    with pytest.raises(ToolError):
        await read_file("SPEC.md", start_line=10, end_line=1)


async def test_read_file_rejects_nonexistent_file() -> None:
    with pytest.raises(ToolError):
        await read_file("apps/target_app/does_not_exist.py")


async def test_search_code_finds_a_known_string() -> None:
    results = await search_code("ZeroDivisionError")
    assert any(r["path"] == "apps/target_app/bugs.py" for r in results)


async def test_search_code_returns_empty_for_no_matches() -> None:
    # Built by concatenation, not a literal, so `git grep` (which searches
    # tracked file *content*) can never match this test's own source line.
    pattern = "this_pattern_should_never_" + "match_anything_xyz123"
    results = await search_code(pattern)
    assert results == []


async def test_list_files_finds_target_app_python_files() -> None:
    files = await list_files("apps/target_app/*.py")
    assert "apps/target_app/bugs.py" in files


async def test_list_files_never_returns_dotenv() -> None:
    files = await list_files("*")
    assert ".env" not in files


# --- git_blame / recent_commits ------------------------------------------------


async def test_get_git_blame_on_a_real_file() -> None:
    result = await get_git_blame("SPEC.md", 1)
    assert "commit" in result


async def test_get_git_blame_rejects_dotenv() -> None:
    with pytest.raises(ToolError):
        await get_git_blame(".env", 1)


async def test_get_recent_commits_returns_history() -> None:
    commits = await get_recent_commits(3)
    assert 1 <= len(commits) <= 3
    assert all(len(c["sha"]) == 40 for c in commits)


# --- run_tests -----------------------------------------------------------------


async def test_run_tests_in_a_worktree(git_worktree: tuple[str, Path]) -> None:
    name, worktree_path = git_worktree
    scratch = worktree_path / "tests" / "test_mcp_tools_code_scratch.py"
    scratch.write_text("def test_ok():\n    assert 1 + 1 == 2\n", encoding="utf-8")

    result = await run_tests(worktree=name, test_path="tests/test_mcp_tools_code_scratch.py")
    assert result["passed"] is True


async def test_run_tests_rejects_unknown_worktree() -> None:
    with pytest.raises(ToolError):
        await run_tests(worktree="not-a-real-worktree")


# --- propose_patch: the safety-critical one -----------------------------------


async def test_propose_patch_runtime_job_can_modify_target_app(
    git_worktree: tuple[str, Path],
) -> None:
    name, worktree_path = git_worktree
    job_id = await _make_heal_job(HealJobType.RUNTIME_ERROR)
    diff = _diff_for(worktree_path, "apps/target_app/bugs.py", "# a fix comment\n")

    result = await propose_patch(heal_job_id=job_id, worktree=name, unified_diff=diff)

    assert result["applied"] is True
    assert "apps/target_app/bugs.py" in result["files_changed"]
    content = (worktree_path / "apps/target_app/bugs.py").read_text(encoding="utf-8")
    assert content.startswith("# a fix comment\n")


async def test_propose_patch_runtime_job_cannot_modify_outside_target_app(
    git_worktree: tuple[str, Path],
) -> None:
    name, worktree_path = git_worktree
    job_id = await _make_heal_job(HealJobType.RUNTIME_ERROR)
    diff = _diff_for(worktree_path, "sentinel/storage.py", "# should never land\n")

    with pytest.raises(ToolError):
        await propose_patch(heal_job_id=job_id, worktree=name, unified_diff=diff)

    # the file must be untouched - the tool must reject before applying anything
    content = (worktree_path / "sentinel/storage.py").read_text(encoding="utf-8")
    assert "should never land" not in content


async def test_propose_patch_contract_violation_job_is_also_target_app_only(
    git_worktree: tuple[str, Path],
) -> None:
    name, worktree_path = git_worktree
    job_id = await _make_heal_job(HealJobType.CONTRACT_VIOLATION)
    diff = _diff_for(worktree_path, "core/config.py", "# should never land\n")

    with pytest.raises(ToolError):
        await propose_patch(heal_job_id=job_id, worktree=name, unified_diff=diff)


async def test_propose_patch_ci_failure_job_can_modify_outside_target_app(
    git_worktree: tuple[str, Path],
) -> None:
    name, worktree_path = git_worktree
    job_id = await _make_heal_job(HealJobType.CI_FAILURE)
    diff = _diff_for(worktree_path, "sentinel/storage.py", "# a ci fix comment\n")

    result = await propose_patch(heal_job_id=job_id, worktree=name, unified_diff=diff)

    assert result["applied"] is True
    content = (worktree_path / "sentinel/storage.py").read_text(encoding="utf-8")
    assert content.startswith("# a ci fix comment\n")


async def test_propose_patch_never_allows_forbidden_paths_regardless_of_job_type(
    git_worktree: tuple[str, Path],
) -> None:
    name, worktree_path = git_worktree
    job_id = await _make_heal_job(HealJobType.CI_FAILURE)
    diff = (
        "--- a/.github/workflows/ci.yml\n"
        "+++ b/.github/workflows/ci.yml\n"
        "@@ -1,1 +1,1 @@\n"
        "-name: CI\n"
        "+name: Hacked\n"
    )

    with pytest.raises(ToolError):
        await propose_patch(heal_job_id=job_id, worktree=name, unified_diff=diff)


async def test_propose_patch_rejects_unknown_heal_job() -> None:
    with pytest.raises(ToolError):
        await propose_patch(heal_job_id=2**62, worktree="whatever", unified_diff="--- a\n+++ b\n")


async def test_propose_patch_rejects_a_diff_that_does_not_apply(
    git_worktree: tuple[str, Path],
) -> None:
    name, _ = git_worktree
    job_id = await _make_heal_job(HealJobType.CI_FAILURE)
    bogus_diff = (
        "--- a/apps/target_app/bugs.py\n"
        "+++ b/apps/target_app/bugs.py\n"
        "@@ -1,1 +1,1 @@\n"
        "-this text is not actually in the file\n"
        "+neither is this\n"
    )

    with pytest.raises(ToolError):
        await propose_patch(heal_job_id=job_id, worktree=name, unified_diff=bogus_diff)


async def test_propose_patch_scopes_a_multi_app_job_to_its_own_app(
    git_worktree: tuple[str, Path],
) -> None:
    """A runtime_error job for app B can't be scoped to app A's paths just
    because A is the default -- its own registered app determines scope."""
    name, worktree_path = git_worktree
    app_id = await _make_monitored_app(
        allowed_write_paths=["apps/target_app/routes.py"], test_command="pytest"
    )
    job_id = await _make_heal_job(HealJobType.RUNTIME_ERROR, app_id=app_id)

    # allowed for this app (its own scoped path)
    ok_diff = _diff_for(worktree_path, "apps/target_app/routes.py", "# scoped ok\n")
    result = await propose_patch(heal_job_id=job_id, worktree=name, unified_diff=ok_diff)
    assert result["applied"] is True

    # rejected: bugs.py is not in *this* app's allowed_write_paths, even
    # though it's under apps/target_app/ (the legacy single-app default)
    bad_diff = _diff_for(worktree_path, "apps/target_app/bugs.py", "# should never land\n")
    with pytest.raises(ToolError):
        await propose_patch(heal_job_id=job_id, worktree=name, unified_diff=bad_diff)


async def test_run_tests_uses_the_apps_own_test_command_for_non_python_apps(
    git_worktree: tuple[str, Path],
) -> None:
    name, worktree_path = git_worktree
    app_dir = worktree_path / "examples" / "scratch_app"
    app_dir.mkdir(parents=True)
    app_id = await _make_monitored_app(
        allowed_write_paths=["examples/scratch_app/"],
        test_command="node -e \"console.log('ok'); process.exit(0)\"",
    )
    job_id = await _make_heal_job(HealJobType.RUNTIME_ERROR, app_id=app_id)

    result = await run_tests(worktree=name, heal_job_id=job_id)

    assert result["passed"] is True
    assert "ok" in result["output"]


@pytest.mark.parametrize(("exit_code", "expected"), [(0, True), (3, False)])
async def test_run_tests_full_suite_runs_a_python_apps_configured_command(
    git_worktree: tuple[str, Path], exit_code: int, expected: bool
) -> None:
    """The pre-PR gate (no test_path) runs the app's test_command, not all of pytest."""
    name, _ = git_worktree
    app_id = await _make_monitored_app(
        allowed_write_paths=["apps/x/"],
        test_command=f"python -c \"print('configured suite'); raise SystemExit({exit_code})\"",
        language="python",
    )
    job_id = await _make_heal_job(HealJobType.RUNTIME_ERROR, app_id=app_id)

    result = await run_tests(worktree=name, heal_job_id=job_id)

    assert result["passed"] is expected
    assert "configured suite" in result["output"]


def test_pytest_command_uses_the_current_interpreter() -> None:
    import sys

    from mcp_server.git_utils import pytest_command

    assert pytest_command("pytest a b").startswith(f'"{sys.executable}" -m pytest a b')
    assert pytest_command("go test ./...") == "go test ./..."

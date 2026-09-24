"""mcp_server.git_utils: git blame/log/apply and pytest-in-a-worktree.

Runs against the real repo (read-only ops) and a real, disposable
`git worktree` (write ops) via the `git_worktree` fixture.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from mcp_server import git_utils
from mcp_server.sandbox import REPO_ROOT


async def test_recent_commits_returns_real_history() -> None:
    commits = await git_utils.recent_commits(2, cwd=REPO_ROOT)
    assert len(commits) == 2
    for commit in commits:
        assert len(commit["sha"]) == 40
        assert commit["author"]
        assert commit["subject"]


async def test_blame_line_on_a_real_tracked_file() -> None:
    info = await git_utils.blame_line("SPEC.md", 1, cwd=REPO_ROOT)
    assert len(info["commit"]) == 40
    assert "author" in info


async def test_blame_line_on_a_nonexistent_file_raises() -> None:
    with pytest.raises(git_utils.GitCommandError):
        await git_utils.blame_line("does/not/exist.py", 1, cwd=REPO_ROOT)


def _real_diff_for_prepending_a_line(worktree_path: Path, relative_path: str, line: str) -> str:
    """Make a real edit, capture `git diff` for it, then revert - so the
    returned text is a guaranteed-valid unified diff (not hand-crafted)."""
    target_file = worktree_path / relative_path
    original = target_file.read_text(encoding="utf-8")
    target_file.write_text(line + original, encoding="utf-8")

    diff_text = subprocess.run(  # noqa: S603
        ["git", "diff", "--", relative_path],  # noqa: S607
        cwd=str(worktree_path),
        capture_output=True,
        text=True,
        check=True,
    ).stdout

    subprocess.run(  # noqa: S603
        ["git", "checkout", "--", relative_path],  # noqa: S607
        cwd=str(worktree_path),
        check=True,
        capture_output=True,
    )
    assert target_file.read_text(encoding="utf-8") == original
    return diff_text


async def test_apply_diff_and_run_pytest_in_a_real_worktree(
    git_worktree: tuple[str, Path],
) -> None:
    _, worktree_path = git_worktree
    relative_path = "apps/target_app/seed_data.py"
    diff_text = _real_diff_for_prepending_a_line(
        worktree_path, relative_path, "# a harmless marker comment added by a test\n"
    )

    await git_utils.apply_diff(diff_text, cwd=worktree_path, check_only=True)
    await git_utils.apply_diff(diff_text, cwd=worktree_path, check_only=False)

    updated = (worktree_path / relative_path).read_text(encoding="utf-8")
    assert updated.startswith("# a harmless marker comment added by a test\n")

    stat = await git_utils.diff_stat(cwd=worktree_path)
    assert "seed_data.py" in stat


async def test_apply_diff_reverse_undoes_the_patch(git_worktree: tuple[str, Path]) -> None:
    _, worktree_path = git_worktree
    relative_path = "apps/target_app/seed_data.py"
    diff_text = _real_diff_for_prepending_a_line(worktree_path, relative_path, "# marker\n")

    await git_utils.apply_diff(diff_text, cwd=worktree_path, check_only=False)
    assert (worktree_path / relative_path).read_text(encoding="utf-8").startswith("# marker\n")

    await git_utils.apply_diff(diff_text, cwd=worktree_path, reverse=True)

    stat = await git_utils.diff_stat(cwd=worktree_path)
    assert stat.strip() == ""  # working tree is clean again


async def test_apply_diff_rejects_a_diff_that_does_not_match(
    git_worktree: tuple[str, Path],
) -> None:
    _, worktree_path = git_worktree
    bogus_diff = (
        "--- a/apps/target_app/seed_data.py\n"
        "+++ b/apps/target_app/seed_data.py\n"
        "@@ -1,3 +1,3 @@\n"
        "-this line definitely does not exist in the file\n"
        "+neither does this replacement\n"
    )
    with pytest.raises(git_utils.GitCommandError):
        await git_utils.apply_diff(bogus_diff, cwd=worktree_path, check_only=True)


async def test_run_pytest_passes_for_a_trivially_passing_test(
    git_worktree: tuple[str, Path],
) -> None:
    _, worktree_path = git_worktree
    scratch_test = worktree_path / "tests" / "test_mcp_git_utils_scratch.py"
    scratch_test.write_text("def test_always_passes():\n    assert True\n", encoding="utf-8")

    result = await git_utils.run_pytest("tests/test_mcp_git_utils_scratch.py", cwd=worktree_path)

    assert result["passed"] is True
    assert result["timed_out"] is False


async def test_run_pytest_reports_failure_for_a_failing_test(
    git_worktree: tuple[str, Path],
) -> None:
    _, worktree_path = git_worktree
    scratch_test = worktree_path / "tests" / "test_mcp_git_utils_scratch_fail.py"
    scratch_test.write_text("def test_always_fails():\n    assert False\n", encoding="utf-8")

    result = await git_utils.run_pytest(
        "tests/test_mcp_git_utils_scratch_fail.py", cwd=worktree_path
    )

    assert result["passed"] is False
    assert "test_always_fails" in result["output"]

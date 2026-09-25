"""healer.worktree: git worktree create/reset/commit-push/remove lifecycle."""

from __future__ import annotations

import asyncio
import uuid

from healer.worktree import (
    branch_name_for,
    commit_and_push,
    create_worktree,
    create_worktree_for_branch,
    remove_worktree,
    reset_worktree,
)
from mcp_server.sandbox import REPO_ROOT


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


async def _git_run(*args: str, cwd: str = str(REPO_ROOT)) -> None:
    """Run a `git` command for its effect, without blocking the event loop."""
    process = await asyncio.create_subprocess_exec(
        "git",
        *args,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await process.communicate()
    assert process.returncode == 0, stderr.decode()


def test_branch_name_is_short_and_job_scoped() -> None:
    assert branch_name_for("abcdef0123456789fingerprint", 42) == "autofix/abcdef012345-42"


async def _seed_fake_remote_main(remote: str) -> None:
    """Push the local repo's current HEAD to `remote` as its `main` branch,
    so `create_worktree(..., remote=remote)`'s default `<remote>/main` base
    has something to resolve against without ever touching the real
    `origin`."""
    await _git_run("push", remote, "HEAD:refs/heads/main")


async def test_create_and_remove_worktree_lifecycle(fake_git_remote: str) -> None:
    await _seed_fake_remote_main(fake_git_remote)
    name = f"test-{uuid.uuid4().hex[:8]}"
    branch = f"autofix-test/{name}"

    path = await create_worktree(name, branch, remote=fake_git_remote)
    try:
        assert path.is_dir()
        assert (path / "pyproject.toml").is_file()
    finally:
        await remove_worktree(name, branch)

    assert not path.exists()
    branches = await _git_stdout("branch", "--list", branch)
    assert branch not in branches


async def test_reset_worktree_discards_uncommitted_changes(fake_git_remote: str) -> None:
    await _seed_fake_remote_main(fake_git_remote)
    name = f"test-{uuid.uuid4().hex[:8]}"
    branch = f"autofix-test/{name}"
    path = await create_worktree(name, branch, remote=fake_git_remote)
    try:
        marker = path / "apps" / "target_app" / "_scratch_marker.txt"
        marker.write_text("uncommitted scratch content", encoding="utf-8")
        assert marker.exists()

        await reset_worktree(path)

        assert not marker.exists()
    finally:
        await remove_worktree(name, branch)


async def test_commit_and_push_pushes_to_the_given_remote(fake_git_remote: str) -> None:
    await _seed_fake_remote_main(fake_git_remote)
    name = f"test-{uuid.uuid4().hex[:8]}"
    branch = f"autofix-test/{name}"
    path = await create_worktree(name, branch, remote=fake_git_remote)
    try:
        new_file = path / "apps" / "target_app" / "_scratch_marker.txt"
        new_file.write_text("a committed change", encoding="utf-8")

        await commit_and_push(path, branch, message="test: scratch commit", remote=fake_git_remote)

        log = await _git_stdout("log", f"{fake_git_remote}/{branch}", "-1", "--pretty=%s")
        assert "test: scratch commit" in log
    finally:
        await remove_worktree(name, branch)


async def test_create_worktree_fetches_and_bases_off_the_remotes_latest_main(
    fake_git_remote: str,
) -> None:
    """PR #10 regression: a worktree must be based on `<remote>/main` as of a
    fresh `fetch`, not on whatever the local `main` ref already happened to
    point at (which a long-running healer process's own checkout can leave
    arbitrarily stale). Simulated here by pushing local HEAD to the fake
    remote's `main`, then advancing the fake remote's `main` *again* to a new
    commit the local repo has never seen — the new worktree must land on that
    newer commit, proving it actually fetched, not just used its own local
    knowledge of `main`."""
    await _seed_fake_remote_main(fake_git_remote)

    ahead_branch = f"ahead-test/{uuid.uuid4().hex[:8]}"
    marker_relpath = "apps/target_app/_pr10_regression_marker.txt"
    await _git_run("branch", ahead_branch, "HEAD")
    await _git_run("worktree", "add", f"worktrees/{ahead_branch.replace('/', '-')}", ahead_branch)
    ahead_worktree = REPO_ROOT / "worktrees" / ahead_branch.replace("/", "-")
    try:
        (ahead_worktree / marker_relpath).write_text("origin moved ahead", encoding="utf-8")
        await _git_run("add", marker_relpath, cwd=str(ahead_worktree))
        await _git_run("commit", "-m", "test: advance fake remote main", cwd=str(ahead_worktree))
        await _git_run(
            "push",
            "--force",
            fake_git_remote,
            f"{ahead_branch}:refs/heads/main",
            cwd=str(ahead_worktree),
        )
    finally:
        await _git_run("worktree", "remove", "--force", str(ahead_worktree))
        await _git_run("branch", "-D", ahead_branch)

    assert not (REPO_ROOT / marker_relpath).exists(), "local main must never see this commit"

    name = f"test-{uuid.uuid4().hex[:8]}"
    branch = f"autofix-test/{name}"
    path = await create_worktree(name, branch, remote=fake_git_remote)
    try:
        assert (path / marker_relpath).is_file()
    finally:
        await remove_worktree(name, branch)


async def test_create_worktree_for_branch_checks_out_an_existing_remote_branch(
    fake_git_remote: str,
) -> None:
    """A branch that exists only on the remote (no local ref) — simulating a
    PR branch pushed by an earlier, separate process — must still be
    checkable-out via fetch + git's remote-tracking DWIM behavior."""
    branch = f"pr-test/{uuid.uuid4().hex[:8]}"
    await _git_run("branch", branch, "main")
    await _git_run("push", fake_git_remote, branch)
    await _git_run("branch", "-D", branch)

    name = f"test-{uuid.uuid4().hex[:8]}"
    path = await create_worktree_for_branch(name, branch, remote=fake_git_remote)
    try:
        assert path.is_dir()
        assert (path / "pyproject.toml").is_file()
        current_branch = await _git_stdout("-C", str(path), "rev-parse", "--abbrev-ref", "HEAD")
        assert current_branch.strip() == branch
    finally:
        await remove_worktree(name, branch)

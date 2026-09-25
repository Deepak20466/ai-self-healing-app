"""Git worktree lifecycle for one fix attempt.

SPEC.md: the healer uses "git worktrees for isolated fix attempts". Creating/
resetting/removing a worktree is *not* an MCP tool — `mcp_server/sandbox.py`'s
`resolve_worktree_dir` only validates a worktree name that already exists;
starting a fix attempt (and cleaning up after it) is the healer's own
responsibility, done with plain `git` subprocess calls exactly like
`mcp_server/git_utils.py` does for the tools that operate on an existing one.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from mcp_server.sandbox import REPO_ROOT, WORKTREES_ROOT

GIT_TIMEOUT_SECONDS = 30.0


class WorktreeError(Exception):
    """Raised when creating, resetting, or removing a git worktree fails."""


async def _run_git(args: list[str], *, cwd: Path) -> None:
    process = await asyncio.create_subprocess_exec(
        "git",
        *args,
        cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, stderr = await asyncio.wait_for(process.communicate(), timeout=GIT_TIMEOUT_SECONDS)
    except TimeoutError as exc:
        process.kill()
        await process.wait()
        raise WorktreeError(f"git {' '.join(args)} timed out") from exc
    if process.returncode != 0:
        raise WorktreeError(f"git {' '.join(args)} failed: {stderr.decode(errors='replace')}")


def branch_name_for(fingerprint: str, heal_job_id: int) -> str:
    """`autofix/<fingerprint-short>-<job-id>`.

    The job id makes every branch unique even when the same fingerprint gets
    a new heal_job after a prior failed attempt, so it never collides with a
    leftover branch/PR from that earlier attempt.
    """
    return f"autofix/{fingerprint[:12]}-{heal_job_id}"


async def create_worktree(name: str, branch: str, *, base: str = "main") -> Path:
    """`git worktree add -b <branch> worktrees/<name> <base>`. Returns the path."""
    WORKTREES_ROOT.mkdir(exist_ok=True)
    path = WORKTREES_ROOT / name
    await _run_git(["worktree", "add", "-b", branch, str(path), base], cwd=REPO_ROOT)
    return path


async def create_worktree_for_branch(name: str, branch: str, *, remote: str = "origin") -> Path:
    """Check out an *existing* branch (a PR's branch) into a new worktree.

    Used by the CI-fix loop, which fixes forward on the same PR branch
    rather than opening a new one (SPEC.md: "fix it in a worktree of the
    same PR branch"). Fetches `branch` from `remote` first so a local ref
    always exists to check out — `git worktree add <path> <branch>` then
    auto-creates a local branch tracking `<remote>/<branch>` (the same DWIM
    behavior `git checkout <branch>` has for an unambiguous remote branch),
    exactly like a fresh clone would.
    """
    WORKTREES_ROOT.mkdir(exist_ok=True)
    path = WORKTREES_ROOT / name
    await _run_git(["fetch", remote, branch], cwd=REPO_ROOT)
    await _run_git(["worktree", "add", str(path), branch], cwd=REPO_ROOT)
    return path


async def reset_worktree(path: Path) -> None:
    """Discard uncommitted changes, back to a clean HEAD.

    Called between failed fix-attempt iterations so one bad patch never
    leaks into the next attempt's diff.
    """
    await _run_git(["reset", "--hard", "HEAD"], cwd=path)
    await _run_git(["clean", "-fd"], cwd=path)


async def commit_and_push(path: Path, branch: str, *, message: str, remote: str = "origin") -> None:
    """Stage everything, commit, and push `branch` to `remote` (default `origin`).

    `remote` is overridable so tests can push to a throwaway local bare repo
    (see `tests/conftest.py`'s `fake_git_remote` fixture) instead of the real
    `origin` — a worktree shares its `.git` config (and therefore its
    remotes) with the main repo, so this is the only way to keep test pushes
    off the public GitHub repo.
    """
    await _run_git(["add", "-A"], cwd=path)
    await _run_git(["commit", "-m", message], cwd=path)
    await _run_git(["push", "-u", remote, branch], cwd=path)


async def _run_git_best_effort(args: list[str], *, cwd: Path) -> None:
    """Like `_run_git`, but for cleanup: never raises, and never blocks past
    `GIT_TIMEOUT_SECONDS` even if the process itself won't die.

    Hit this for real: on Windows, a just-exited child process (the nested
    `pytest` subprocess `run_tests` spawns inside the worktree) can leave a
    file handle inside the worktree directory open for a moment after
    `communicate()` returns, and `git worktree remove --force` then blocks
    on that file lock — with no timeout, that stalled the entire worker (and
    the whole test suite) indefinitely. `process.kill()` on timeout releases
    the *git* process; if the underlying file lock itself doesn't clear, the
    directory is simply left behind (see the docstring above: a leftover
    worktree is a minor annoyance, never a correctness problem).
    """
    process = await asyncio.create_subprocess_exec(
        "git",
        *args,
        cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        await asyncio.wait_for(process.communicate(), timeout=GIT_TIMEOUT_SECONDS)
    except TimeoutError:
        process.kill()
        await process.wait()


async def remove_worktree(name: str, branch: str) -> None:
    """Best-effort cleanup: remove the worktree and delete the local branch ref.

    Never raises — this runs in a `finally` block, and a leftover worktree
    directory is a minor annoyance, not a correctness problem. The branch may
    already be pushed to `origin` (its PR, if any, is unaffected by deleting
    the *local* ref).
    """
    path = WORKTREES_ROOT / name
    await _run_git_best_effort(["worktree", "remove", "--force", str(path)], cwd=REPO_ROOT)
    await _run_git_best_effort(["branch", "-D", branch], cwd=REPO_ROOT)

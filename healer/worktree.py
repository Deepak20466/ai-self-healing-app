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
import shutil
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


async def create_worktree(
    name: str, branch: str, *, base: str | None = None, remote: str = "origin"
) -> Path:
    """`git fetch <remote>`, then `git worktree add -b <branch> worktrees/<name> <base>`.

    Always fetches first and bases the new branch off `<remote>/main` (default
    `origin/main`), not the local `main` — which is whatever this
    long-running process's own checkout happened to have at startup and can
    be arbitrarily far behind. Without this, an autofix branch created hours
    after the process started (or after the process pulled a big batch of
    unrelated commits) forks from a stale point, so its PR's diff includes
    every commit `origin/main` gained since then on top of the real fix —
    reproduced for real as PR #10's diff showing dozens of unrelated files
    instead of just the actual 2-file fix. `base` is overridable only for
    tests that need to assert against a specific known commit; production
    code never passes it.
    """
    if base is None:
        base = f"{remote}/main"
    WORKTREES_ROOT.mkdir(exist_ok=True)
    path = WORKTREES_ROOT / name
    await _run_git(["fetch", remote], cwd=REPO_ROOT)
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


async def create_worktree_for_connected_app(name: str, branch: str, *, source_dir: Path) -> Path:
    """Clone a connect-a-repo app's own local checkout (`connected_apps/<name>/`,
    see `core/repo_connect.py`) into a fresh worktree and check out a new
    branch there.

    Unlike `create_worktree`, this is *not* `git worktree add` against this
    project's own `.git` -- a connected app is a real, independent git
    repository in its own right (its own `.git/`, its own history, its own
    GitHub remote), just physically nested under this repo's working tree
    and gitignored. `git worktree add` only ever materializes commits that
    exist in *this* repo's object store, so it would produce an empty
    directory for anything under `connected_apps/`. A plain local `git
    clone` (fast: no network, same filesystem) gives the fix attempt its own
    isolated copy exactly the way `create_worktree` does for an in-repo app.
    """
    WORKTREES_ROOT.mkdir(exist_ok=True)
    path = WORKTREES_ROOT / name
    await _run_git(["clone", str(source_dir), str(path)], cwd=WORKTREES_ROOT)
    await _run_git(["checkout", "-b", branch], cwd=path)
    return path


async def remove_plain_clone(name: str) -> None:
    """Best-effort cleanup for a `create_worktree_for_connected_app` clone --
    not a real git-worktree of this repo, so `git worktree remove` doesn't
    apply; just delete the directory (same best-effort, never-raises
    contract as `remove_worktree`)."""
    path = WORKTREES_ROOT / name

    def _rmtree() -> None:
        shutil.rmtree(path, ignore_errors=True)

    await asyncio.get_running_loop().run_in_executor(None, _rmtree)


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

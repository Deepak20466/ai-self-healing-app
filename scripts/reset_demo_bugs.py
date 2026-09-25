"""Restore the 7 seeded demo bugs (SPEC.md app-pod section) to their
original, reproducible-broken state.

Why this exists: the healer's whole job is to find and fix these bugs (see
PR #10, which fixed bug #5 for real, in free mode). If a PR like that one is
ever merged into `main`, the seeded bug it fixed stops reproducing, and
every future `/trigger/*` demo of that bug (and the sentinel prober test
that catches it) silently breaks. This script is the reset switch: restore
`apps/target_app/bugs.py` and the three test files that assert each bug's
*broken* behavior (`tests/test_target_app_bugs.py`,
`tests/test_target_app_routes.py`, `tests/test_sentinel_prober.py`) back to
the pristine snapshots in `scripts/demo_bug_originals/`.

`apps/target_app/contracts.py` is deliberately NOT restored: its `expected`
values are always the *correct* answer regardless of whether a bug is fixed
or broken (that's what lets the prober detect a violation in the first
place) — it never needs resetting.

Never pushes to `main` directly (SPEC.md/CLAUDE.md's guardrail against a
script silently rewriting the trunk): the default mode creates a disposable
git worktree off `origin/main`, commits the restored files on a
`demo-reset` branch, force-pushes only that branch, and opens (or updates)
a PR for a human to review and merge. Use `--local` during active demo
development to just rewrite the files in this checkout, with no git/GitHub
calls at all.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ORIGINALS_DIR = Path(__file__).resolve().parent / "demo_bug_originals"
BRANCH_NAME = "demo-reset"
PR_TITLE = "Reset demo bugs"

#: repo-relative path -> snapshot filename under scripts/demo_bug_originals/
RESTORE_TARGETS: dict[str, str] = {
    "apps/target_app/bugs.py": "bugs.py.orig",
    "tests/test_target_app_bugs.py": "test_target_app_bugs.py.orig",
    "tests/test_target_app_routes.py": "test_target_app_routes.py.orig",
    "tests/test_sentinel_prober.py": "test_sentinel_prober.py.orig",
}


def restore_files(target_root: Path) -> list[str]:
    """Overwrite each target under `target_root` with its pristine snapshot.

    Returns the repo-relative paths that actually changed (so callers can
    tell "nothing to reset" from "restored N files" and only `git add`
    what's real).
    """
    changed: list[str] = []
    for rel_path, snapshot_name in RESTORE_TARGETS.items():
        snapshot = ORIGINALS_DIR / snapshot_name
        # Raw bytes, not read_text/write_text: those do newline translation
        # (LF -> os.linesep on write), which would silently turn the repo's
        # LF line endings into CRLF on Windows and show up as a spurious
        # whole-file diff even though nothing meaningful changed.
        original_bytes = snapshot.read_bytes()
        dst = target_root / rel_path
        if dst.exists() and dst.read_bytes() == original_bytes:
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(original_bytes)
        changed.append(rel_path)
    return changed


def _run(args: list[str], *, cwd: Path) -> None:
    subprocess.run(args, cwd=str(cwd), check=True)


def _run_ok(args: list[str], *, cwd: Path) -> bool:
    """Like `_run`, but returns False instead of raising on failure."""
    return subprocess.run(args, cwd=str(cwd), check=False).returncode == 0


def _open_pr_url(worktree: Path) -> str | None:
    """The URL of an already-open PR for `BRANCH_NAME`, if one exists."""
    result = subprocess.run(
        ["gh", "pr", "list", "--head", BRANCH_NAME, "--json", "url", "--jq", ".[0].url"],
        cwd=str(worktree),
        check=True,
        capture_output=True,
        text=True,
    )
    url = result.stdout.strip()
    return url or None


def reset_via_pr() -> int:
    """Fetch, restore on a disposable worktree/branch, push, open a PR."""
    _run(["git", "fetch", "origin"], cwd=REPO_ROOT)

    worktree = REPO_ROOT / "worktrees" / "demo-reset-wt"
    if worktree.exists():
        _run_ok(["git", "worktree", "remove", "--force", str(worktree)], cwd=REPO_ROOT)
        shutil.rmtree(worktree, ignore_errors=True)  # belt: --force can leave the dir on Windows
    _run_ok(["git", "branch", "-D", BRANCH_NAME], cwd=REPO_ROOT)  # ok if it doesn't exist

    _run(
        ["git", "worktree", "add", "-b", BRANCH_NAME, str(worktree), "origin/main"],
        cwd=REPO_ROOT,
    )
    try:
        changed = restore_files(worktree)
        if not changed:
            print("origin/main is already at the original seeded-bug state; nothing to reset.")
            return 0

        _run(["git", "add", *changed], cwd=worktree)
        _run(
            ["git", "commit", "-m", "Reset demo bugs to their original seeded state"],
            cwd=worktree,
        )
        _run(["git", "push", "--force", "origin", f"HEAD:{BRANCH_NAME}"], cwd=worktree)

        existing_url = _open_pr_url(worktree)
        if existing_url:
            print(f"Restored {len(changed)} file(s); updated existing PR: {existing_url}")
        else:
            _run(
                [
                    "gh",
                    "pr",
                    "create",
                    "--title",
                    PR_TITLE,
                    "--body",
                    (
                        "Restores the 7 seeded demo bugs in `apps/target_app/` (and the tests "
                        "that assert each one's broken behavior) to their original state, so "
                        "every `/trigger/*` endpoint keeps reproducing them for future demos. "
                        "Generated by `scripts/reset_demo_bugs.py`."
                    ),
                    "--head",
                    BRANCH_NAME,
                    "--base",
                    "main",
                ],
                cwd=worktree,
            )
            print(f"Restored {len(changed)} file(s); opened PR '{PR_TITLE}'.")
        return 0
    finally:
        _run_ok(["git", "worktree", "remove", "--force", str(worktree)], cwd=REPO_ROOT)
        shutil.rmtree(worktree, ignore_errors=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--local",
        action="store_true",
        help="Only restore files in this checkout; no git fetch/branch/push, no PR.",
    )
    args = parser.parse_args()

    if args.local:
        changed = restore_files(REPO_ROOT)
        if not changed:
            print("Already at the original seeded-bug state; nothing to restore.")
        else:
            print(f"Restored {len(changed)} file(s):")
            for path in changed:
                print(f"  {path}")
        return 0

    return reset_via_pr()


if __name__ == "__main__":
    sys.exit(main())

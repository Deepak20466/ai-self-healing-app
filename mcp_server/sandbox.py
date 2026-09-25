"""Filesystem sandbox for every mcp_server tool that touches disk.

Two separate policies, per SPEC.md's mcp-pod section:
  - **read** access: block secrets (`.env`, `.env.local`, ... but not the
    non-secret `.env.example`) and raw `.git/` internals.
  - **write** access: block those plus `.git/`, `.github/workflows/`, and
    `alembic/versions/` (SPEC.md's explicit forbidden-write list), and —
    separately — runtime auto-fixes (`propose_patch` for a `runtime_error`/
    `contract_violation` heal_job) may additionally be restricted to
    `apps/target_app/` only, so the healer can never modify its own
    infrastructure. That restriction is looked up from the heal_job's `type`
    in the database (see `tools/code.py`), never trusted from a caller-
    supplied parameter — SPEC.md's "guardrails are enforced in code, not
    only in prompts" applies here too.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKTREES_ROOT = REPO_ROOT / "worktrees"

MAX_READ_FILE_BYTES = 200_000
MAX_WRITE_FILE_BYTES = 500_000

_FORBIDDEN_WRITE_PREFIXES = (".git/", ".github/workflows/", "alembic/versions/")
#: Legacy default write scope for a runtime_error/contract_violation job
#: with no app_id (predates multi-app support). A job with an app_id uses
#: that app's own `allowed_write_paths` instead (see tools/code.py).
RUNTIME_FIX_ALLOWED_PREFIX = "apps/target_app/"

_DIFF_PATH_PATTERN = re.compile(r"^(?:---|\+\+\+) (?:a/|b/)?(?P<path>\S+)", re.MULTILINE)


class SandboxViolation(Exception):
    """Raised for any attempted access outside the allowed sandbox."""


def _is_dotenv_secret(relative_posix: str) -> bool:
    name = relative_posix.rsplit("/", 1)[-1]
    return name == ".env" or (name.startswith(".env.") and name != ".env.example")


def resolve_repo_path(relative_path: str) -> Path:
    """Resolve `relative_path` against the repo root, rejecting any escape."""
    candidate = (REPO_ROOT / relative_path).resolve()
    try:
        candidate.relative_to(REPO_ROOT)
    except ValueError as exc:
        raise SandboxViolation(f"Path escapes the repo root: {relative_path!r}") from exc
    return candidate


def to_repo_relative(path: Path) -> str:
    return path.resolve().relative_to(REPO_ROOT).as_posix()


def check_readable(relative_path: str) -> Path:
    """Resolve and validate a path for reading. Raises SandboxViolation if forbidden."""
    resolved = resolve_repo_path(relative_path)
    rel = to_repo_relative(resolved)
    if _is_dotenv_secret(rel):
        raise SandboxViolation(f"Reading {rel!r} is not allowed (secrets file)")
    if rel == ".git" or rel.startswith(".git/"):
        raise SandboxViolation(f"Reading {rel!r} is not allowed (git internals)")
    return resolved


def check_writable(relative_path: str, *, allowed_prefixes: list[str] | None = None) -> Path:
    """Resolve and validate a path for writing. Raises SandboxViolation if forbidden.

    `allowed_prefixes`, when given, additionally restricts writes to paths
    starting with one of them (a monitored app's `allowed_write_paths`, or
    the legacy single-app `apps/target_app/` default for jobs with no
    app_id).
    """
    resolved = resolve_repo_path(relative_path)
    rel = to_repo_relative(resolved)

    if _is_dotenv_secret(rel):
        raise SandboxViolation(f"Writing to {rel!r} is not allowed (secrets file)")
    for forbidden in _FORBIDDEN_WRITE_PREFIXES:
        if rel == forbidden.rstrip("/") or rel.startswith(forbidden):
            raise SandboxViolation(f"Writing to {rel!r} is not allowed (forbidden path)")
    if allowed_prefixes is not None and not any(rel.startswith(p) for p in allowed_prefixes):
        raise SandboxViolation(
            f"Writing to {rel!r} is outside the allowed scope {allowed_prefixes!r}"
        )
    return resolved


def resolve_worktree_dir(worktree: str) -> Path:
    """Resolve a worktree directory name under WORKTREES_ROOT, sandboxed."""
    if not worktree or "/" in worktree or "\\" in worktree or worktree in (".", ".."):
        raise SandboxViolation(f"Invalid worktree name: {worktree!r}")
    resolved = (WORKTREES_ROOT / worktree).resolve()
    try:
        resolved.relative_to(WORKTREES_ROOT)
    except ValueError as exc:
        raise SandboxViolation(f"Invalid worktree name: {worktree!r}") from exc
    if not resolved.is_dir():
        raise SandboxViolation(f"Worktree does not exist: {worktree!r}")
    return resolved


def extract_diff_paths(unified_diff: str) -> set[str]:
    """Extract every file path touched by a unified diff (from ---/+++ headers)."""
    paths = set()
    for match in _DIFF_PATH_PATTERN.finditer(unified_diff):
        path = match.group("path")
        if path == "/dev/null":
            continue
        paths.add(path)
    return paths


def check_diff_paths_writable(
    unified_diff: str, *, allowed_prefixes: list[str] | None = None
) -> set[str]:
    """Validate every path touched by a diff against the write sandbox.

    Returns the set of touched (repo-relative) paths on success; raises
    SandboxViolation naming the first offending path otherwise.
    """
    touched = extract_diff_paths(unified_diff)
    if not touched:
        raise SandboxViolation("Diff does not touch any files")
    for path in touched:
        check_writable(path, allowed_prefixes=allowed_prefixes)
    return touched

"""Code tools: read_file, search_code, list_files, get_git_blame,
get_recent_commits, run_tests, propose_patch.

`run_tests`/`propose_patch` operate on an explicit `worktree` name (a
directory under `worktrees/`, created by the healer via `git worktree add`
before it starts a fix attempt) rather than implicit per-session state —
simpler to reason about and test, and the calling agent already knows which
worktree it's working in.
"""

from __future__ import annotations

import asyncio
from typing import Any

from core.db import session_scope
from core.models import HealJob, HealJobType
from mcp_server import git_utils
from mcp_server.audit import audited_tool
from mcp_server.instance import mcp
from mcp_server.sandbox import (
    MAX_READ_FILE_BYTES,
    MAX_WRITE_FILE_BYTES,
    REPO_ROOT,
    RUNTIME_FIX_ALLOWED_PREFIX,
    SandboxViolation,
    check_diff_paths_writable,
    check_readable,
    resolve_worktree_dir,
    to_repo_relative,
)
from mcp_server.tools._exceptions import ToolError

_NOISE_DIR_PREFIXES = (
    ".git/",
    ".venv/",
    "venv/",
    "__pycache__",
    ".mypy_cache/",
    ".ruff_cache/",
    ".pytest_cache/",
    "worktrees/",
    "node_modules/",
    "htmlcov/",
)


def _is_noise_path(relative_posix: str) -> bool:
    return (
        any(
            relative_posix == prefix.rstrip("/") or relative_posix.startswith(prefix)
            for prefix in _NOISE_DIR_PREFIXES
        )
        or "/__pycache__/" in relative_posix
    )


@audited_tool(mcp, "read_file")
async def read_file(
    path: str, start_line: int | None = None, end_line: int | None = None
) -> dict[str, Any]:
    """Read a file (optionally a 1-indexed inclusive line range), capped at 200KB."""
    try:
        resolved = check_readable(path)
    except SandboxViolation as exc:
        raise ToolError(str(exc)) from exc

    if not resolved.is_file():
        raise ToolError(f"Not a file: {path!r}")

    raw = resolved.read_bytes()
    if start_line is None and end_line is None and len(raw) > MAX_READ_FILE_BYTES:
        raise ToolError(
            f"{path!r} is {len(raw)} bytes, over the {MAX_READ_FILE_BYTES}-byte read limit; "
            "pass start_line/end_line to read a slice instead"
        )

    text = raw.decode("utf-8", errors="replace")
    lines = text.splitlines()
    total_lines = len(lines)

    start = max(1, start_line or 1)
    end = min(total_lines, end_line or total_lines)
    if start > end:
        raise ToolError(f"start_line {start} is after end_line {end}")

    selected = "\n".join(lines[start - 1 : end])
    if len(selected.encode("utf-8")) > MAX_READ_FILE_BYTES:
        raise ToolError(f"Requested range is over the {MAX_READ_FILE_BYTES}-byte read limit")

    return {
        "path": path,
        "start_line": start,
        "end_line": end,
        "total_lines": total_lines,
        "content": selected,
    }


@audited_tool(mcp, "search_code")
async def search_code(pattern: str, max_results: int = 100) -> list[dict[str, Any]]:
    """Regex search across tracked files (`git grep -nIE`), never matches gitignored files."""
    process = await asyncio.create_subprocess_exec(
        "git",
        "grep",
        "-nIE",
        "--max-count",
        str(max_results),
        pattern,
        cwd=str(REPO_ROOT),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()

    if process.returncode == 1:  # git grep: no matches
        return []
    if process.returncode not in (0, 1):
        raise ToolError(f"search_code failed: {stderr.decode(errors='replace')}")

    results: list[dict[str, Any]] = []
    for line in stdout.decode(errors="replace").splitlines()[:max_results]:
        file_path, _, remainder = line.partition(":")
        line_number_str, _, content = remainder.partition(":")
        if _is_noise_path(file_path):
            continue
        try:
            line_number = int(line_number_str)
        except ValueError:
            continue
        results.append({"path": file_path, "line": line_number, "content": content})
    return results


@audited_tool(mcp, "list_files")
async def list_files(glob: str = "**/*") -> list[str]:
    """List repo-relative paths matching `glob` (e.g. `apps/target_app/**/*.py`)."""
    matches = []
    for candidate in REPO_ROOT.glob(glob):
        if not candidate.is_file():
            continue
        rel = to_repo_relative(candidate)
        if _is_noise_path(rel) or rel == ".env" or rel.startswith(".env."):
            continue
        matches.append(rel)
    return sorted(matches)


@audited_tool(mcp, "get_git_blame")
async def get_git_blame(path: str, line: int) -> dict[str, str]:
    """`git blame` for a single line: commit, author, date, summary."""
    try:
        check_readable(path)
    except SandboxViolation as exc:
        raise ToolError(str(exc)) from exc

    try:
        return await git_utils.blame_line(path, line, cwd=REPO_ROOT)
    except git_utils.GitCommandError as exc:
        raise ToolError(str(exc)) from exc


@audited_tool(mcp, "get_recent_commits")
async def get_recent_commits(n: int = 10) -> list[dict[str, str]]:
    """Last `n` commits: sha, author, date, subject."""
    try:
        return await git_utils.recent_commits(n, cwd=REPO_ROOT)
    except git_utils.GitCommandError as exc:
        raise ToolError(str(exc)) from exc


@audited_tool(mcp, "run_tests")
async def run_tests(worktree: str | None = None, test_path: str | None = None) -> dict[str, Any]:
    """Run pytest with a timeout, in `worktree` if given, else the main repo (read-only run)."""
    try:
        cwd = resolve_worktree_dir(worktree) if worktree else REPO_ROOT
    except SandboxViolation as exc:
        raise ToolError(str(exc)) from exc

    return await git_utils.run_pytest(test_path, cwd=cwd)


@audited_tool(mcp, "propose_patch")
async def propose_patch(heal_job_id: int, worktree: str, unified_diff: str) -> dict[str, Any]:
    """Validate and apply a unified diff to `worktree` only.

    The write scope is derived from `heal_job_id`'s `type` in the database —
    never from a caller-supplied parameter — so a runtime_error/
    contract_violation job can only ever touch `apps/target_app/`, while a
    ci_failure job may touch anything outside the universal forbidden paths
    (.env, .git/, .github/workflows/, alembic/versions/).
    """
    async with session_scope() as session:
        job = await session.get(HealJob, heal_job_id)
        if job is None:
            raise ToolError(f"No heal_job with id {heal_job_id}")
        job_type = job.type

    allowed_prefix = (
        RUNTIME_FIX_ALLOWED_PREFIX
        if job_type in (HealJobType.RUNTIME_ERROR, HealJobType.CONTRACT_VIOLATION)
        else None
    )

    try:
        touched_paths = check_diff_paths_writable(unified_diff, allowed_prefix=allowed_prefix)
        worktree_dir = resolve_worktree_dir(worktree)
    except SandboxViolation as exc:
        raise ToolError(str(exc)) from exc

    try:
        await git_utils.apply_diff(unified_diff, cwd=worktree_dir, check_only=True)
    except git_utils.GitCommandError as exc:
        raise ToolError(f"Diff does not apply cleanly: {exc}") from exc

    await git_utils.apply_diff(unified_diff, cwd=worktree_dir, check_only=False)

    oversized = [
        path
        for path in touched_paths
        if (worktree_dir / path).exists()
        and (worktree_dir / path).stat().st_size > MAX_WRITE_FILE_BYTES
    ]
    if oversized:
        await git_utils.apply_diff(unified_diff, cwd=worktree_dir, reverse=True)
        raise ToolError(f"Patch rejected: file(s) over {MAX_WRITE_FILE_BYTES} bytes: {oversized}")

    stat = await git_utils.diff_stat(cwd=worktree_dir)

    return {
        "applied": True,
        "heal_job_id": heal_job_id,
        "worktree": worktree,
        "files_changed": sorted(touched_paths),
        "diff_stat": stat.strip(),
    }

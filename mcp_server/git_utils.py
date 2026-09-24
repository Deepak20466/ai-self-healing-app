"""Subprocess wrappers around `git` (blame, log, apply) and `pytest`.

Every function takes an explicit `cwd` — callers (tools/code.py) resolve
that path through `sandbox.py` first, so nothing here ever receives an
un-sandboxed path.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

GIT_TIMEOUT_SECONDS = 30.0
TEST_TIMEOUT_SECONDS = 120.0


class GitCommandError(Exception):
    """Raised when a `git` subprocess fails or times out."""


async def _run_git(args: list[str], cwd: Path, timeout: float = GIT_TIMEOUT_SECONDS) -> str:
    process = await asyncio.create_subprocess_exec(
        "git",
        *args,
        cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except TimeoutError as exc:
        process.kill()
        await process.wait()
        raise GitCommandError(f"git {' '.join(args)} timed out after {timeout}s") from exc

    if process.returncode != 0:
        raise GitCommandError(f"git {' '.join(args)} failed: {stderr.decode(errors='replace')}")
    return stdout.decode(errors="replace")


async def blame_line(path: str, line: int, *, cwd: Path) -> dict[str, str]:
    """`git blame` for a single line, parsed from `--porcelain` output."""
    output = await _run_git(["blame", "-L", f"{line},{line}", "--porcelain", "--", path], cwd)
    return _parse_blame_porcelain(output)


def _parse_blame_porcelain(output: str) -> dict[str, str]:
    lines = output.splitlines()
    if not lines:
        raise GitCommandError("git blame returned no output")

    info: dict[str, str] = {"commit": lines[0].split()[0]}
    for line in lines[1:]:
        if line.startswith("author "):
            info["author"] = line.removeprefix("author ")
        elif line.startswith("author-time "):
            info["author_time"] = line.removeprefix("author-time ")
        elif line.startswith("summary "):
            info["summary"] = line.removeprefix("summary ")
        elif line.startswith("\t"):
            info["content"] = line[1:]
            break
    return info


_LOG_FIELD_SEPARATOR = "\x1f"


async def recent_commits(n: int, *, cwd: Path) -> list[dict[str, str]]:
    """Last `n` commits as {sha, author, date, subject}."""
    fmt = _LOG_FIELD_SEPARATOR.join(["%H", "%an", "%aI", "%s"])
    output = await _run_git(["log", f"-n{n}", f"--pretty=format:{fmt}"], cwd)

    commits: list[dict[str, str]] = []
    for line in output.splitlines():
        if not line:
            continue
        sha, author, date, subject = line.split(_LOG_FIELD_SEPARATOR)
        commits.append({"sha": sha, "author": author, "date": date, "subject": subject})
    return commits


async def apply_diff(
    unified_diff: str, *, cwd: Path, check_only: bool = False, reverse: bool = False
) -> None:
    """`git apply` (or, with `reverse=True`, undo) a unified diff against `cwd`.

    Raises GitCommandError if it doesn't apply (or doesn't reverse-apply).
    """
    args = ["apply", "--whitespace=nowarn"]
    if check_only:
        args.append("--check")
    if reverse:
        args.append("--reverse")

    process = await asyncio.create_subprocess_exec(
        "git",
        *args,
        cwd=str(cwd),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await process.communicate(input=unified_diff.encode("utf-8"))
    if process.returncode != 0:
        raise GitCommandError(f"git apply failed: {stderr.decode(errors='replace')}")


async def diff_stat(*, cwd: Path) -> str:
    """`git diff --stat` for the worktree's currently unstaged changes."""
    return await _run_git(["diff", "--stat"], cwd)


async def run_pytest(
    test_path: str | None, *, cwd: Path, timeout: float = TEST_TIMEOUT_SECONDS
) -> dict[str, Any]:
    """Run pytest in `cwd` (a worktree) with a timeout. Returns a JSON-able summary."""
    args = [sys.executable, "-m", "pytest", "-q"]
    if test_path:
        args.append(test_path)

    process = await asyncio.create_subprocess_exec(
        *args,
        cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except TimeoutError:
        process.kill()
        await process.wait()
        return {
            "passed": False,
            "timed_out": True,
            "output": f"Tests timed out after {timeout}s",
        }

    output = stdout.decode(errors="replace")
    return {
        "passed": process.returncode == 0,
        "timed_out": False,
        "return_code": process.returncode,
        "output": output[-20_000:],
    }

"""Shared subprocess-launch plumbing for the "free" (no-API-key) AI backends.

`healer/agent_free.py` (the Claude Code CLI backend, built in Phase 5+) is
the reference implementation for driving a local coding-agent CLI: prompt
via stdin, restrict it to only the selfheal MCP server, strip cloud API
keys from its environment, apply a timeout, parse its JSON result, and
detect "not logged in"/"usage limit" failures from its output. `healer/
agent_codex.py` and `healer/agent_gemini.py` need the exact same plumbing
for two different CLIs (OpenAI's `codex` and Google's `gemini`), so this
module factors out the parts that have nothing to do with which specific
CLI is being launched:

- `kill_process_tree` / `is_windows_shim` / `windows_shim_command`: the
  Windows `.cmd`/`.bat`-shim workaround `agent_free.py` discovered the hard
  way (see its module docstring and `CLAUDE.md`'s "Post-Phase-8" log) is a
  property of *any* npm-installed Node CLI on Windows, not something
  specific to `claude` — `codex`/`gemini` are also commonly npm-installed,
  so the same `create_subprocess_shell` + `cmd.exe /d /s /c "..."` handling
  applies verbatim.
- A generic `CLIError` exception hierarchy (`CLINotFoundError`/
  `CLINotLoggedInError`/`CLIUsageLimitError`/`CLITimeoutError`/
  `CLIMalformedOutputError`), parameterized by which backend raised it, so
  `healer/worker.py`'s `except` handling and any shared retry logic can
  treat all three free-mode backends uniformly without a string-matching
  `isinstance` per module.
- `strip_env_vars`: builds a subprocess environment with a given set of
  variable names removed — used by each backend to strip not just
  `ANTHROPIC_*` but the *other* backends' own API-key-style env vars too,
  so a machine configured for more than one backend never leaks the wrong
  provider's credentials into a CLI subprocess that authenticates via its
  own separate (usually browser/subscription) login.

Design choice: this module intentionally does NOT own the actual argv-
building, JSON-shape parsing, or MCP-restriction mechanism for any specific
CLI — those differ enough between `claude`/`codex`/`gemini` (different
flags, different JSON shapes, different ways of restricting MCP/tool
access) that forcing one shared abstraction over them would obscure more
than it'd save. `agent_free.py` itself is left untouched (it predates this
module and is a load-bearing, already-tested Phase 5+ module) — only the
two new backends import from here.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import subprocess
import sys

logger_name = __name__


class CLIError(Exception):
    """Base class for every free-mode CLI backend failure mode."""


class CLINotFoundError(CLIError):
    """The CLI executable could not be launched at all."""


class CLINotLoggedInError(CLIError):
    """The CLI ran but reported it has no valid login/credentials."""


class CLIUsageLimitError(CLIError):
    """The CLI ran but reported a usage/rate limit was hit."""


class CLITimeoutError(CLIError):
    """The CLI did not finish within the configured timeout."""


class CLIMalformedOutputError(CLIError):
    """The CLI exited but stdout was not the JSON its `--output-format`/`--json` flag promises."""


async def kill_process_tree(pid: int) -> None:
    """Best-effort: kill `pid` and its descendants.

    See `healer/agent_free.py:_kill_process_tree`'s docstring for why this
    is necessary at all on Windows (a `cmd.exe` shim wrapper means plain
    `Process.kill()` only signals the wrapper, not the real child).
    """
    if sys.platform == "win32":
        proc = await asyncio.create_subprocess_exec(
            "taskkill",
            "/F",
            "/T",
            "/PID",
            str(pid),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()
    else:
        import signal

        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGKILL)


def is_windows_shim(resolved: str) -> bool:
    return sys.platform == "win32" and resolved.lower().endswith((".cmd", ".bat"))


def windows_shim_command(resolved: str, rest_args: list[str]) -> str:
    """Build the full `cmd.exe /d /s /c "..."` command string for a `.cmd`/`.bat` shim.

    Identical logic to `healer/agent_free.py:_windows_shim_command` — see
    that function's docstring for the full "why" (two-pass `list2cmdline`
    corruption, and `cmd.exe /c`'s own quoting quirks with spaces in the
    path). Duplicated here rather than imported from `agent_free` so this
    module has no dependency on that one (keeps the free-mode backends
    independent of each other; `agent_free.py` is not touched by this
    session's work).
    """
    inner = subprocess.list2cmdline([resolved, *rest_args])
    return f'cmd.exe /d /s /c "{inner}"'


def strip_env_vars(extra_stripped: frozenset[str]) -> dict[str, str]:
    """`os.environ` with `extra_stripped` (case-insensitive) removed."""
    stripped_upper = {name.upper() for name in extra_stripped}
    return {key: value for key, value in os.environ.items() if key.upper() not in stripped_upper}

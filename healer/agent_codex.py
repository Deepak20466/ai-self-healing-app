"""Free-mode AI backend: drives the fix loop through the local OpenAI Codex
CLI (`codex`) instead of the `anthropic` SDK or the Claude Code CLI.

**Read `healer/agent_free.py`'s module docstring first** — this module is
structurally the same thing for a different CLI, and everything that
docstring says about *why* free mode looks different from `runtime_agent.py`
(the CLI manages its own tool-call loop; this process only sees the final
result; guardrails live entirely in the MCP server, not here) applies
verbatim. Only the CLI-invocation specifics differ, documented below.

**STATUS: untested against a real `codex` install.** Neither `codex` nor
`gemini` is installed on the machine this was built on (`codex --help`/
`gemini --help` both fail with "command not found" — checked directly before
writing this module, not assumed). Everything below is built from Codex
CLI's public documentation (`developers.openai.com/codex/cli/reference`,
`developers.openai.com/codex/noninteractive`) as of this session
(2026-09-26), not from a live `--help` dump. If you have `codex` installed,
the first thing to do before trusting this module is re-run this session's
own live-verification steps (see `CLAUDE.md`'s dated entry for this work)
against it and fix anything documented-but-wrong.

**Non-interactive invocation**: `codex exec` is Codex's scriptable mode (no
TUI, no interactive prompts). The prompt is piped via stdin using `codex exec
-` (the literal `-` argument tells it to read the prompt from stdin rather
than argv — same "never put the prompt in argv" rationale as
`agent_free.py`). `--json` makes it emit **newline-delimited JSON events**
(one per state change: `agent_message`, `task_complete`, tool-call events,
etc.) to stdout, not one single JSON object the way Claude Code's
`--output-format json` does — this module reads and parses every line,
keeping the last `task_complete`/`agent_message`-shaped one as the final
result and treating a stdout with zero parseable JSON lines as malformed
output.

**MCP restriction — a real, documented gap, not just this module being
cautious.** Codex CLI has no per-invocation equivalent of Claude Code's
`--allowedTools`/`--disallowedTools`: MCP servers are configured in
`~/.codex/config.toml` (or a `CODEX_HOME`-relative one), and there is no flag
to say "only this one MCP server, and disable your own built-in
edit/apply_patch/shell tools." Two further wrinkles, cited from OpenAI's own
docs/issue tracker (see the module-level search this session did — not
independently re-verified against a live install):
1. In `codex exec` non-interactive mode, MCP tool calls that would normally
   need interactive approval are auto-cancelled (stdin is closed, so the
   approval prompt has nothing to read) *unless* the approval policy is
   relaxed — see openai/codex issue #24135.
2. The only broadly-documented way to relax that is `--dangerously-bypass-
   approvals-and-sandbox`, which disables sandboxing (filesystem/network
   restrictions) for *every* tool call, not just MCP ones — unacceptable
   here per this project's "guardrails live in the MCP server, never
   weakened by the agent layer" rule.

This module's mitigation, in lieu of a real allow/deny-list flag: spawn
`codex exec` with `-c approval_policy="never" -c sandbox_mode="read-only"`
via a dedicated `CODEX_HOME` (an isolated temp dir populated with a
`config.toml` that defines *only* the `selfheal` MCP server, pointed at
mcp-pod's HTTP endpoint — mirroring `.mcp.json`'s role for Claude Code, see
`agent_free.py`'s docstring for why HTTP, not a spawned stdio server, is
required from inside a worktree `cwd`). `approval_policy=never` removes the
interactive-approval requirement (so MCP tool calls the model attempts
actually run) without touching the sandbox; `sandbox_mode=read-only` means
Codex's own built-in file-write/shell-write tools can observe but cannot
modify anything on disk — the *only* way this agent can make a change that
survives is by calling `propose_patch` through the MCP server, which is
exactly the restriction this codebase needs. This is a deliberate, narrower
alternative to the documented `--dangerously-bypass-approvals-and-sandbox`
footgun, not confirmed to behave this way against a live `codex` binary.
**If you install `codex` and find `sandbox_mode="read-only"` blocks MCP tool
calls too (not just the built-in ones), or the config keys/section names
differ from what's implemented below, fix this module and update this
docstring — do not silently switch to the bypass flag.**

Everything else mirrors `agent_free.py` exactly: prompt via stdin, `codex`
executable resolution + the same Windows `.cmd`/`.bat`-shim handling
(factored into `healer/cli_common.py`, shared with `agent_gemini.py`), a
timeout with process-tree kill on expiry, stripping cloud-API env vars
(`ANTHROPIC_*` plus, here, `GOOGLE_API_KEY`/`GEMINI_API_KEY` — Codex
authenticates via its own ChatGPT/API-key login, never those), regex
detection of "not logged in"/"usage limit" phrasing in stdout+stderr, and
independent post-hoc verification (`_verify_and_summarize`, imported from
`agent_free.py` since it has zero backend-specific logic — it only inspects
the worktree's own diff and calls the shared `run_tests` MCP tool).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import shutil
import tempfile
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import structlog

from core.config import settings
from core.db import session_scope
from core.models import AuditLog, FixAttempt, HealJob, HealJobStatus, HealJobType, MonitoredApp
from healer import agent_free, ci_agent
from healer.circuit_breaker import fingerprint_circuit_open
from healer.cli_common import (
    CLIError,
    CLIMalformedOutputError,
    CLINotFoundError,
    CLINotLoggedInError,
    CLITimeoutError,
    CLIUsageLimitError,
    is_windows_shim,
    kill_process_tree,
    strip_env_vars,
    windows_shim_command,
)
from healer.github_ops import (
    CIFixOutcome,
    FixEvidence,
    open_ci_needs_human_issue,
    open_fix_pull_request,
    open_low_confidence_issue,
    post_ci_fix_comment,
)
from healer.mcp_client import MCPToolClient
from healer.worktree import (
    branch_name_for,
    commit_and_push,
    create_worktree,
    create_worktree_for_branch,
    create_worktree_for_connected_app,
    remove_plain_clone,
    remove_worktree,
    reset_worktree,
)
from mcp_server.github_client import GitHubClient
from mcp_server.sandbox import REPO_ROOT

logger = structlog.get_logger(__name__)

MAX_ATTEMPTS = 3
_MCP_HTTP_URL = f"http://127.0.0.1:{settings.mcp_port}/mcp"

# Codex authenticates via its own ChatGPT/API-key login; these are every
# *other* backend's credential-style env var, stripped so a machine
# configured for more than one backend never leaks the wrong provider's
# secret into this subprocess.
_STRIPPED_ENV_VARS = frozenset(
    {
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_MODEL",
        "GOOGLE_API_KEY",
        "GEMINI_API_KEY",
    }
)

_USAGE_LIMIT_PATTERN = re.compile(
    r"usage limit|rate.?limit exceeded|quota exceeded|resets? at \d|exceeded.*usage",
    re.IGNORECASE,
)
_NOT_LOGGED_IN_PATTERN = re.compile(
    r"not logged in|please run.{0,20}login|not authenticated|no credentials found|"
    r"invalid api key|unauthorized|authentication (?:failed|required)|please authenticate|"
    r"run `?codex login`?",
    re.IGNORECASE,
)

# Infra-level failures: log and treat the attempt as failed, but keep the job
# alive to retry (mirrors agent_free.py's _CLI_INFRA_ERRORS).
_CLI_INFRA_ERRORS = (CLINotFoundError, CLITimeoutError, CLIMalformedOutputError)

_CLI_INVOCATION_ACTION = "cli_invocation"


# Reuse agent_free.py's CLIResult dataclass verbatim rather than defining a
# structurally-identical one here: `_verify_and_summarize` (imported below
# from agent_free.py, since it has zero backend-specific logic) is typed
# against that exact class, and every field this module needs (result_text,
# is_error, token counts, cost) is already backend-agnostic - "codex exec
# --json"'s JSONL final event is parsed into this same shape in
# run_codex_cli below, just from a different wire format than Claude Code's.
CLIResult = agent_free.CLIResult


def _codex_config_dir(mcp_config_dir: Path) -> None:
    """Write a `CODEX_HOME`-style `config.toml` restricted to the selfheal MCP server.

    See this module's docstring for why `sandbox_mode="read-only"` +
    `approval_policy="never"` is used instead of Codex's documented
    `--dangerously-bypass-approvals-and-sandbox` bypass flag.
    """
    config_toml = (
        f'approval_policy = "never"\n'
        f'sandbox_mode = "read-only"\n'
        f"\n"
        f"[mcp_servers.selfheal]\n"
        f'url = "{_MCP_HTTP_URL}"\n'
    )
    (mcp_config_dir / "config.toml").write_text(config_toml, encoding="utf-8")


async def run_codex_cli(
    prompt: str,
    *,
    cwd: Path,
    max_turns: int | None = None,
    timeout_s: int | None = None,
    cli_path: str | None = None,
) -> CLIResult:
    """Run one non-interactive `codex exec` turn and parse its JSONL result stream.

    Mirrors `healer/agent_free.py:run_claude_cli` in every way that isn't
    Codex-specific (stdin prompt, Windows shim handling, timeout, env
    stripping, not-logged-in/usage-limit detection) — see this module's
    docstring for what *is* different (JSONL output, config-file-based MCP
    restriction instead of `--allowedTools`).
    """
    resolved_cli = shutil.which(cli_path or settings.codex_cli_path or "codex") or (
        cli_path or settings.codex_cli_path or "codex"
    )
    turns = max_turns if max_turns is not None else settings.codex_cli_max_turns
    timeout = timeout_s if timeout_s is not None else settings.codex_cli_timeout_s

    with tempfile.TemporaryDirectory(prefix="selfheal-codex-home-") as codex_home_str:
        codex_home = Path(codex_home_str)
        _codex_config_dir(codex_home)

        rest_args = ["exec", "-", "--json", "-c", f"max_turns={turns}"]
        env = strip_env_vars(_STRIPPED_ENV_VARS)
        env["CODEX_HOME"] = str(codex_home)
        subprocess_kwargs: dict[str, Any] = {
            "cwd": str(cwd),
            "env": env,
            "stdin": asyncio.subprocess.PIPE,
            "stdout": asyncio.subprocess.PIPE,
            "stderr": asyncio.subprocess.PIPE,
        }

        try:
            if is_windows_shim(resolved_cli):
                command = windows_shim_command(resolved_cli, rest_args)
                process = await asyncio.create_subprocess_shell(command, **subprocess_kwargs)
            else:
                process = await asyncio.create_subprocess_exec(
                    resolved_cli, *rest_args, **subprocess_kwargs
                )
        except FileNotFoundError as exc:
            raise CLINotFoundError(
                f"codex CLI not found (tried {resolved_cli!r}); "
                "install it and/or set CODEX_CLI_PATH"
            ) from exc

        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                process.communicate(prompt.encode("utf-8")), timeout=timeout
            )
        except TimeoutError:
            await kill_process_tree(process.pid)
            with contextlib.suppress(Exception):
                await process.wait()
            raise CLITimeoutError(f"codex CLI timed out after {timeout}s") from None

        stdout_text = stdout_bytes.decode("utf-8", errors="replace")
        stderr_text = stderr_bytes.decode("utf-8", errors="replace")
        combined = f"{stdout_text}\n{stderr_text}".strip()

        events: list[dict[str, Any]] = []
        for line in stdout_text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                events.append(parsed)

        if not events:
            if _USAGE_LIMIT_PATTERN.search(combined):
                raise CLIUsageLimitError(combined[-2000:])
            if _NOT_LOGGED_IN_PATTERN.search(combined):
                raise CLINotLoggedInError(combined[-2000:])
            raise CLIMalformedOutputError(
                f"codex CLI produced no JSONL events (exit {process.returncode}): "
                f"{combined[-2000:]}"
            )

        final = events[-1]
        for event in reversed(events):
            if event.get("type") in ("task_complete", "agent_message", "error"):
                final = event
                break

        result_text = str(
            final.get("message") or final.get("text") or final.get("last_agent_message") or ""
        )
        is_error = bool(final.get("type") == "error" or final.get("is_error", False))
        if is_error and _USAGE_LIMIT_PATTERN.search(result_text or combined):
            raise CLIUsageLimitError(result_text or combined[-2000:])
        if is_error and _NOT_LOGGED_IN_PATTERN.search(result_text or combined):
            raise CLINotLoggedInError(result_text or combined[-2000:])

        usage = final.get("usage") or {}
        total_cost = final.get("total_cost_usd")
        return CLIResult(
            result_text=result_text,
            is_error=is_error,
            subtype=str(final.get("type", "")),
            num_turns=len([e for e in events if e.get("type") == "turn_complete"]) or len(events),
            session_id=final.get("session_id") or final.get("conversation_id"),
            input_tokens=int(usage.get("input_tokens", 0) or 0),
            output_tokens=int(usage.get("output_tokens", 0) or 0),
            cache_creation_input_tokens=int(usage.get("cache_creation_input_tokens", 0) or 0),
            cache_read_input_tokens=int(usage.get("cache_read_input_tokens", 0) or 0),
            total_cost_usd=Decimal(str(total_cost)) if total_cost is not None else None,
            raw=final,
        )


# Reuse agent_free.py's daily-CLI-call-count helpers unchanged: they only
# count `cli_invocation` audit_log rows since UTC midnight, which is
# entirely backend-agnostic (this module's own `_record_cli_invocation`
# below writes the same action name). Reusing rather than duplicating also
# means `tests/conftest.py`'s `isolated_cli_call_date` fixture (which
# monkeypatches `healer.agent_free.datetime`) isolates this backend's tests
# too, with no separate fixture needed.
_cli_calls_today = agent_free._cli_calls_today
_is_cli_budget_paused = agent_free._is_cli_budget_paused


async def _record_cli_invocation(
    session: Any, *, heal_job_id: int, cli_result: CLIResult | None, error: str | None
) -> None:
    details: dict[str, Any] = (
        {"error": error, "backend": "codex_cli"} if error else {"backend": "codex_cli"}
    )
    if cli_result is not None:
        details.update(
            {
                "num_turns": cli_result.num_turns,
                "subtype": cli_result.subtype,
                "is_error": cli_result.is_error,
                "total_cost_usd": str(cli_result.total_cost_usd)
                if cli_result.total_cost_usd is not None
                else None,
            }
        )
    session.add(
        AuditLog(
            action=_CLI_INVOCATION_ACTION, actor="healer", heal_job_id=heal_job_id, details=details
        )
    )
    await session.flush()


async def _audit(
    session: Any, *, action: str, heal_job_id: int | None, details: dict[str, Any] | None = None
) -> None:
    session.add(AuditLog(action=action, actor="healer", details=details, heal_job_id=heal_job_id))
    await session.flush()


# Prompts, verification, and the outer fix loop are byte-for-byte the same
# shape as agent_free.py's (see that module for the full rationale) - the
# CLI call itself (run_codex_cli vs run_claude_cli) and its exception types
# are the only backend-specific pieces. Reuse agent_free.py's prompt
# builders and verification helper directly rather than forking them.
_runtime_prompt = agent_free._runtime_prompt
_ci_prompt = agent_free._ci_prompt
_verify_and_summarize = agent_free._verify_and_summarize
_touched_paths = agent_free._touched_paths
_regression_test_path = agent_free._regression_test_path
_rerun_workflow_called_since = agent_free._rerun_workflow_called_since
_AttemptOutcome = agent_free._AttemptOutcome


async def _mark_failed_and_open_issue(
    github: GitHubClient,
    *,
    job_id: int,
    fingerprint: str,
    error_summary: str,
    reason: str,
    attempts_summary: str,
) -> None:
    evidence = FixEvidence(
        heal_job_id=job_id,
        fingerprint=fingerprint,
        root_cause=reason,
        diff_stat="",
        test_output=attempts_summary,
        error_summary=error_summary,
    )
    issue = await open_low_confidence_issue(
        github, evidence=evidence, attempts_summary=attempts_summary
    )
    async with session_scope() as session:
        job = await session.get(HealJob, job_id)
        assert job is not None
        job.status = HealJobStatus.FAILED
        job.error_message = reason
        job.finished_at = datetime.now(UTC)
        await _audit(
            session,
            action="heal_failed",
            heal_job_id=job_id,
            details={"reason": reason, "issue_number": issue.get("number")},
        )


async def run_heal_job_codex(
    job_id: int,
    *,
    mcp: MCPToolClient,
    github: GitHubClient,
    remote: str = "origin",
) -> None:
    """Codex-CLI-backed equivalent of `healer.agent_free.run_heal_job_free`.

    Same interface, same outer shape (circuit breaker, up to `MAX_ATTEMPTS`
    attempts each running one Codex CLI turn, PR on success or a fallback
    issue when attempts are exhausted, independent post-hoc verification of
    whatever the CLI claims) — see this module's and `agent_free.py`'s
    docstrings for what's shared vs. Codex-specific.
    """
    async with session_scope() as session:
        job = await session.get(HealJob, job_id)
        if job is None:
            logger.warning("agent_codex.job_not_found", heal_job_id=job_id)
            return
        fingerprint = job.fingerprint
        job_type = job.type
        source_error_id = job.source_error_id
        source_contract_violation_id = job.source_contract_violation_id
        app = await session.get(MonitoredApp, job.app_id) if job.app_id is not None else None
        is_connected_app = app is not None and app.repo_url is not None

        if await fingerprint_circuit_open(
            session, fingerprint, max_attempts=settings.max_heal_attempts_per_fingerprint_24h
        ):
            job.status = HealJobStatus.FAILED
            job.error_message = (
                "circuit breaker: too many heal attempts for this fingerprint in 24h"
            )
            job.finished_at = datetime.now(UTC)
            await _audit(
                session,
                action="circuit_breaker_tripped",
                heal_job_id=job_id,
                details={"fingerprint": fingerprint},
            )
            return

    if job_type == HealJobType.RUNTIME_ERROR:
        source_kind = "error"
        source = await mcp.call_tool("get_error", {"error_id": source_error_id})
        error_summary = (
            f"{source['exception_type']} in {source['file_path']}:{source['line_number']}"
        )
    elif job_type == HealJobType.CONTRACT_VIOLATION:
        source_kind = "contract_violation"
        source = await mcp.call_tool(
            "get_contract_violation", {"violation_id": source_contract_violation_id}
        )
        error_summary = (
            f"contract violation at {source['endpoint']} "
            f"({source['file_path']}:{source['line_number']})"
        )
    else:
        raise ValueError(
            f"run_heal_job_codex only handles runtime_error/contract_violation, got {job_type}"
        )

    worktree_name = f"heal-{job_id}"
    branch = branch_name_for(fingerprint, job_id)
    if is_connected_app:
        assert app is not None
        worktree_path = await create_worktree_for_connected_app(
            worktree_name, branch, source_dir=REPO_ROOT / app.local_repo_path
        )
    else:
        worktree_path = await create_worktree(worktree_name, branch)

    attempts_summaries: list[str] = []
    previous_summary: str | None = None

    try:
        for attempt_number in range(1, MAX_ATTEMPTS + 1):
            async with session_scope() as session:
                if await _is_cli_budget_paused(session):
                    job = await session.get(HealJob, job_id)
                    assert job is not None
                    job.status = HealJobStatus.PAUSED_BUDGET
                    await _audit(
                        session,
                        action="budget_paused",
                        heal_job_id=job_id,
                        details={"category": "cli_calls_per_day"},
                    )
                    return

            prompt = _runtime_prompt(
                source_kind=source_kind,
                source=source,
                attempt_number=attempt_number,
                max_attempts=MAX_ATTEMPTS,
                heal_job_id=job_id,
                worktree=worktree_name,
                previous_attempts_summary=previous_summary,
            )

            try:
                cli_result = await run_codex_cli(prompt, cwd=worktree_path)
            except CLINotLoggedInError as exc:
                async with session_scope() as session:
                    await _record_cli_invocation(
                        session, heal_job_id=job_id, cli_result=None, error=str(exc)
                    )
                await _mark_failed_and_open_issue(
                    github,
                    job_id=job_id,
                    fingerprint=fingerprint,
                    error_summary=error_summary,
                    reason=f"Codex CLI is not logged in: {exc}",
                    attempts_summary="\n\n".join(attempts_summaries),
                )
                return
            except CLIUsageLimitError as exc:
                async with session_scope() as session:
                    await _record_cli_invocation(
                        session, heal_job_id=job_id, cli_result=None, error=str(exc)
                    )
                    job = await session.get(HealJob, job_id)
                    assert job is not None
                    job.status = HealJobStatus.PAUSED_BUDGET
                    await _audit(
                        session,
                        action="budget_paused",
                        heal_job_id=job_id,
                        details={"category": "codex_usage_limit", "detail": str(exc)},
                    )
                return
            except _CLI_INFRA_ERRORS as exc:
                logger.error("agent_codex.cli_call_failed", heal_job_id=job_id, error=str(exc))
                async with session_scope() as session:
                    await _record_cli_invocation(
                        session, heal_job_id=job_id, cli_result=None, error=str(exc)
                    )
                attempts_summaries.append(f"Attempt {attempt_number}: CLI failure: {exc}")
                previous_summary = attempts_summaries[-1]
                if attempt_number < MAX_ATTEMPTS:
                    await reset_worktree(worktree_path)
                continue

            async with session_scope() as session:
                await _record_cli_invocation(
                    session, heal_job_id=job_id, cli_result=cli_result, error=None
                )

            outcome = await _verify_and_summarize(
                mcp=mcp,
                worktree_path=worktree_path,
                worktree_name=worktree_name,
                cli_result=cli_result,
            )

            async with session_scope() as session:
                job = await session.get(HealJob, job_id)
                assert job is not None
                job.attempt_count = attempt_number
                session.add(
                    FixAttempt(
                        heal_job_id=job_id,
                        attempt_number=attempt_number,
                        root_cause=outcome.root_cause,
                        diff=None,
                        test_output=outcome.test_output,
                        passed=outcome.success,
                        input_tokens=cli_result.input_tokens,
                        output_tokens=cli_result.output_tokens,
                        cached_tokens=cli_result.cache_read_input_tokens,
                        cost_usd=cli_result.total_cost_usd or Decimal("0"),
                    )
                )

            summary = (
                f"Attempt {attempt_number}: {'succeeded' if outcome.success else 'failed'}. "
                f"{outcome.root_cause}\nTest output tail:\n{outcome.test_output[-1000:]}"
            )
            attempts_summaries.append(summary)
            previous_summary = summary

            if outcome.success:
                commit_msg = (
                    f"Auto-fix: {error_summary}\n\nheal_job #{job_id}, fingerprint {fingerprint}"
                )
                base_branch = "main"
                if is_connected_app:
                    assert app is not None
                    push_remote = (
                        f"https://x-access-token:{settings.github_token}"
                        f"@github.com/{app.github_repo}.git"
                    )
                    repo_info = await github.get_repo()
                    base_branch = str(repo_info.get("default_branch") or "main")
                else:
                    push_remote = remote
                await commit_and_push(worktree_path, branch, message=commit_msg, remote=push_remote)
                evidence = FixEvidence(
                    heal_job_id=job_id,
                    fingerprint=fingerprint,
                    root_cause=outcome.root_cause,
                    diff_stat=outcome.diff_stat,
                    test_output=outcome.test_output,
                    error_summary=error_summary,
                )
                pr = await open_fix_pull_request(
                    github, branch=branch, base=base_branch, evidence=evidence
                )
                async with session_scope() as session:
                    job = await session.get(HealJob, job_id)
                    assert job is not None
                    job.status = HealJobStatus.PR_OPENED
                    job.branch_name = branch
                    job.pr_number = pr["number"]
                    await _audit(
                        session,
                        action="pr_opened",
                        heal_job_id=job_id,
                        details={"pr_number": pr["number"], "branch": branch},
                    )
                return

            if attempt_number < MAX_ATTEMPTS:
                await reset_worktree(worktree_path)

        await _mark_failed_and_open_issue(
            github,
            job_id=job_id,
            fingerprint=fingerprint,
            error_summary=error_summary,
            reason="exhausted all fix attempts without a verified passing regression test",
            attempts_summary="\n\n".join(attempts_summaries),
        )
    finally:
        if is_connected_app:
            await remove_plain_clone(worktree_name)
        else:
            await remove_worktree(worktree_name, branch)


async def run_ci_heal_job_codex(
    job_id: int,
    *,
    mcp: MCPToolClient,
    github: GitHubClient,
    remote: str = "origin",
) -> None:
    """Codex-CLI-backed equivalent of `healer.agent_free.run_ci_heal_job_free`.

    Reuses `ci_agent._prepare_job`/`_open_needs_human_issue` unchanged, same
    as the Claude Code free-mode backend does — that logic has no
    Anthropic-specific (or Claude-Code-CLI-specific) behavior at all.
    """
    prepared = await ci_agent._prepare_job(job_id)
    if prepared is None:
        return
    if isinstance(prepared, ci_agent._GiveUp):
        await ci_agent._open_needs_human_issue(
            github, job_id=job_id, pr_number=prepared.pr_number, reason=prepared.reason
        )
        return

    pr_number = prepared.pr_number
    run_id = prepared.run_id
    workflow = prepared.workflow
    branch = prepared.branch
    failed_job_name = prepared.failed_job_name
    attempt_number = prepared.attempt_number
    max_attempts = prepared.max_attempts

    worktree_name = f"ci-heal-{job_id}-{attempt_number}"
    worktree_path = await create_worktree_for_branch(worktree_name, branch, remote=remote)

    try:
        attempt_start = datetime.now(UTC)
        async with session_scope() as session:
            if await _is_cli_budget_paused(session):
                job = await session.get(HealJob, job_id)
                assert job is not None
                job.status = HealJobStatus.PAUSED_BUDGET
                await _audit(
                    session,
                    action="budget_paused",
                    heal_job_id=job_id,
                    details={"category": "cli_calls_per_day"},
                )
                return

        prompt = _ci_prompt(
            run_id=run_id,
            workflow=workflow,
            branch=branch,
            pr_number=pr_number,
            failed_job_name=failed_job_name,
            attempt_number=attempt_number,
            max_attempts=max_attempts,
            heal_job_id=job_id,
            worktree=worktree_name,
        )

        root_cause = ""
        diff_stat = ""
        test_output = ""
        input_tokens = output_tokens = cached_tokens = 0
        cost_usd = Decimal("0")
        outcome = "failed"

        try:
            cli_result = await run_codex_cli(prompt, cwd=worktree_path)
            async with session_scope() as session:
                await _record_cli_invocation(
                    session, heal_job_id=job_id, cli_result=cli_result, error=None
                )
            root_cause = cli_result.result_text or "(agent produced no final summary)"
            input_tokens = cli_result.input_tokens
            output_tokens = cli_result.output_tokens
            cached_tokens = cli_result.cache_read_input_tokens
            cost_usd = cli_result.total_cost_usd or Decimal("0")

            async with session_scope() as session:
                reran = await _rerun_workflow_called_since(
                    session, heal_job_id=job_id, since=attempt_start
                )
            if reran:
                outcome = "flaky_rerun"
            elif not cli_result.is_error:
                verified = await _verify_and_summarize(
                    mcp=mcp,
                    worktree_path=worktree_path,
                    worktree_name=worktree_name,
                    cli_result=cli_result,
                )
                diff_stat = verified.diff_stat
                test_output = verified.test_output
                outcome = "fixed" if verified.success else "failed"
        except CLINotLoggedInError as exc:
            root_cause = f"Codex CLI is not logged in: {exc}"
            async with session_scope() as session:
                await _record_cli_invocation(
                    session, heal_job_id=job_id, cli_result=None, error=str(exc)
                )
        except CLIUsageLimitError as exc:
            async with session_scope() as session:
                await _record_cli_invocation(
                    session, heal_job_id=job_id, cli_result=None, error=str(exc)
                )
                job = await session.get(HealJob, job_id)
                assert job is not None
                job.status = HealJobStatus.PAUSED_BUDGET
                await _audit(
                    session,
                    action="budget_paused",
                    heal_job_id=job_id,
                    details={"category": "codex_usage_limit", "detail": str(exc)},
                )
            return
        except _CLI_INFRA_ERRORS as exc:
            root_cause = f"CLI failure: {exc}"
            async with session_scope() as session:
                await _record_cli_invocation(
                    session, heal_job_id=job_id, cli_result=None, error=str(exc)
                )

        async with session_scope() as session:
            session.add(
                FixAttempt(
                    heal_job_id=job_id,
                    attempt_number=attempt_number,
                    root_cause=root_cause,
                    diff=None,
                    test_output=test_output,
                    passed=outcome == "fixed",
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    cached_tokens=cached_tokens,
                    cost_usd=cost_usd,
                )
            )

        if outcome == "fixed":
            commit_msg = (
                f"CI auto-fix: {workflow} run {run_id}\n\nheal_job #{job_id}, "
                f"attempt {attempt_number}"
            )
            await commit_and_push(worktree_path, branch, message=commit_msg, remote=remote)

        try:
            await post_ci_fix_comment(
                github,
                CIFixOutcome(
                    heal_job_id=job_id,
                    pr_number=pr_number,
                    attempt_number=attempt_number,
                    max_attempts=max_attempts,
                    run_id=run_id,
                    outcome=outcome,
                    root_cause=root_cause,
                    diff_stat=diff_stat,
                    test_output=test_output,
                ),
            )
        except Exception:  # noqa: BLE001 - a comment failure must never crash the worker
            logger.exception("agent_codex.ci_pr_comment_failed", heal_job_id=job_id)

        async with session_scope() as session:
            job = await session.get(HealJob, job_id)
            assert job is not None
            await _audit(
                session,
                action="ci_fix_attempt_finished",
                heal_job_id=job_id,
                details={"outcome": outcome, "run_id": run_id},
            )
            if outcome == "failed":
                job.status = HealJobStatus.FAILED
                job.error_message = "attempt did not produce a passing local test run"
                job.finished_at = datetime.now(UTC)
                await _audit(
                    session,
                    action="heal_failed",
                    heal_job_id=job_id,
                    details={"reason": job.error_message},
                )
            else:
                job.status = HealJobStatus.CI_FIXING

        if outcome == "failed":
            try:
                await open_ci_needs_human_issue(
                    github, pr_number=pr_number, attempts_summary=root_cause
                )
            except Exception:  # noqa: BLE001 - must never crash the worker
                logger.exception("agent_codex.ci_needs_human_issue_failed", heal_job_id=job_id)
    finally:
        await remove_worktree(worktree_name, branch)


__all__ = [
    "CLIError",
    "CLIResult",
    "run_ci_heal_job_codex",
    "run_codex_cli",
    "run_heal_job_codex",
]

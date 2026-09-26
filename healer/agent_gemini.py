"""Free-mode AI backend: drives the fix loop through the local Google Gemini
CLI (`gemini`) instead of the `anthropic` SDK or the Claude Code CLI.

**Read `healer/agent_free.py`'s module docstring first** — same relationship
as `healer/agent_codex.py` has to it: this module is structurally the same
thing for a different CLI. See that module's docstring for the shared "why
this looks different from runtime_agent.py" rationale, which applies here
unchanged.

**STATUS: untested against a real `gemini` install.** Neither `codex` nor
`gemini` is installed on the machine this was built on (`gemini --help`
fails with "command not found" — checked directly before writing this
module). Everything below is built from Gemini CLI's public documentation
(`google-gemini.github.io/gemini-cli`, `geminicli.com/docs`) as of this
session (2026-09-26), not a live `--help` dump. If you have `gemini`
installed, re-run this session's live-verification steps (see `CLAUDE.md`'s
dated entry for this work) against it before trusting this module in
production.

**Non-interactive invocation**: Gemini CLI runs non-interactively whenever
its stdin is piped rather than a TTY (the same "no TTY → no interactive
prompt loop" behavior most CLI coding agents share) — so, like `agent_free.
py`, the prompt is sent via `stdin` (`process.communicate(prompt.encode())`),
never argv. `--output-format json` makes it print one JSON object to stdout
(unlike Codex's JSONL event stream) containing a final `response` string and
a `stats` block with token usage — this module parses that object the same
way `agent_free.py` parses Claude Code's `--output-format json`.

**MCP restriction — settings.json-based, not a CLI flag.** Gemini CLI has no
per-invocation `--allowedTools`/`--mcp-config` flag either; MCP servers and
tool restrictions are both configured via a `settings.json` the CLI reads
from `.gemini/settings.json` in the current working directory (project-level
config) or `~/.gemini/settings.json` (global). Since `run_gemini_cli` is
always invoked with `cwd` set to a fix-attempt's own worktree (a fresh
directory each time), this module writes a **project-level**
`<worktree>/.gemini/settings.json` before every invocation containing:
- `"mcpServers": {"selfheal": {"httpUrl": "http://127.0.0.1:<mcp_port>/mcp"}}`
  — mirrors `.mcp.json`'s HTTP-not-stdio choice in `agent_free.py` (a stdio
  server spawned with this same worktree `cwd` would resolve `mcp_server`'s
  own `REPO_ROOT` to the worktree, not the real repo — see that module's
  docstring for the full "why", which applies here too).
- `"coreTools": []` — Gemini CLI docs describe `coreTools` as an *allowlist*
  of built-in tools (file read/write, shell, web fetch, etc.); an empty list
  allows none of them, leaving the MCP server's tools as the only tools
  available at all. This is a real, documented mechanism (unlike Codex CLI's
  gap — see `agent_codex.py`'s docstring for the contrast), so free mode's
  "only our MCP server, no built-in edit/shell" guarantee should actually
  hold here, modulo not having a live install to confirm against.
- `"mcpServerAllowlist": ["selfheal"]` (or whatever the installed version's
  exact key is — documented under slightly different names across doc
  revisions; if it doesn't exist, `mcpServers` containing only `selfheal`
  already limits which MCP tools exist to call, so this is defense in
  depth, not the only restriction).

Whether Gemini CLI still shows an interactive tool-confirmation dialog for
MCP tool calls with piped/non-TTY stdin (and if so, whether that silently
hangs or auto-declines) is **not verified** — if you install `gemini` and
find MCP tool calls get stuck waiting for approval, look for a
`--yolo`/auto-approve-equivalent flag in that version's `--help` and wire it
in here explicitly (this module does not pass one by default, to avoid
guessing at a flag that might auto-approve more than intended).

Everything else mirrors `agent_free.py`: prompt via stdin, `gemini`
executable resolution + the same Windows `.cmd`/`.bat`-shim handling
(`healer/cli_common.py`, shared with `agent_codex.py`), a timeout with
process-tree kill on expiry, stripping cloud-API env vars (`ANTHROPIC_*`
plus `OPENAI_API_KEY` here — Gemini CLI authenticates via its own Google
account/API-key login, never those), regex detection of "not logged
in"/"usage limit" phrasing, and independent post-hoc verification
(`_verify_and_summarize`, imported from `agent_free.py` unchanged).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import shutil
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

_STRIPPED_ENV_VARS = frozenset(
    {
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_MODEL",
        "OPENAI_API_KEY",
    }
)

_USAGE_LIMIT_PATTERN = re.compile(
    r"usage limit|rate.?limit exceeded|quota exceeded|resets? at \d|exceeded.*usage",
    re.IGNORECASE,
)
_NOT_LOGGED_IN_PATTERN = re.compile(
    r"not logged in|please run.{0,20}login|not authenticated|no credentials found|"
    r"invalid api key|unauthorized|authentication (?:failed|required)|please authenticate",
    re.IGNORECASE,
)

_CLI_INFRA_ERRORS = (CLINotFoundError, CLITimeoutError, CLIMalformedOutputError)
_CLI_INVOCATION_ACTION = "cli_invocation"


# Reuse agent_free.py's CLIResult dataclass verbatim - see agent_codex.py's
# identical comment for the rationale (_verify_and_summarize, imported below
# from agent_free.py, is typed against that exact class).
CLIResult = agent_free.CLIResult


def _write_gemini_settings(worktree_cwd: Path) -> None:
    """Write `<worktree>/.gemini/settings.json`, restricting Gemini to the
    selfheal MCP server only. See this module's docstring for the mechanism
    and its "not verified live" caveat."""
    settings_dir = worktree_cwd / ".gemini"
    settings_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "mcpServers": {"selfheal": {"httpUrl": _MCP_HTTP_URL}},
        "coreTools": [],
        "mcpServerAllowlist": ["selfheal"],
    }
    (settings_dir / "settings.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")


async def run_gemini_cli(
    prompt: str,
    *,
    cwd: Path,
    max_turns: int | None = None,
    timeout_s: int | None = None,
    cli_path: str | None = None,
) -> CLIResult:
    """Run one non-interactive Gemini CLI turn and parse its JSON result.

    Mirrors `healer/agent_free.py:run_claude_cli` in every way that isn't
    Gemini-specific — see this module's docstring for what differs
    (settings.json-based MCP restriction instead of `--allowedTools`, the
    single-JSON-object output shape, which happens to match Claude Code's).
    """
    resolved_cli = shutil.which(cli_path or settings.gemini_cli_path or "gemini") or (
        cli_path or settings.gemini_cli_path or "gemini"
    )
    turns = max_turns if max_turns is not None else settings.gemini_cli_max_turns
    timeout = timeout_s if timeout_s is not None else settings.gemini_cli_timeout_s

    _write_gemini_settings(cwd)

    rest_args = ["--output-format", "json", "--max-turns", str(turns)]
    env = strip_env_vars(_STRIPPED_ENV_VARS)
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
            f"gemini CLI not found (tried {resolved_cli!r}); install it and/or set GEMINI_CLI_PATH"
        ) from exc

    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            process.communicate(prompt.encode("utf-8")), timeout=timeout
        )
    except TimeoutError:
        await kill_process_tree(process.pid)
        with contextlib.suppress(Exception):
            await process.wait()
        raise CLITimeoutError(f"gemini CLI timed out after {timeout}s") from None

    stdout_text = stdout_bytes.decode("utf-8", errors="replace")
    stderr_text = stderr_bytes.decode("utf-8", errors="replace")
    combined = f"{stdout_text}\n{stderr_text}".strip()

    try:
        payload = json.loads(stdout_text)
        if not isinstance(payload, dict):
            raise ValueError("top-level JSON was not an object")
    except (json.JSONDecodeError, ValueError):
        if _USAGE_LIMIT_PATTERN.search(combined):
            raise CLIUsageLimitError(combined[-2000:]) from None
        if _NOT_LOGGED_IN_PATTERN.search(combined):
            raise CLINotLoggedInError(combined[-2000:]) from None
        raise CLIMalformedOutputError(
            f"gemini CLI did not return a JSON object (exit {process.returncode}): "
            f"{combined[-2000:]}"
        ) from None

    result_text = str(payload.get("response") or payload.get("result") or "")
    error_block = payload.get("error")
    is_error = bool(error_block) or bool(payload.get("is_error", False))
    if is_error and _USAGE_LIMIT_PATTERN.search(str(error_block or result_text)):
        raise CLIUsageLimitError(str(error_block or result_text))
    if is_error and _NOT_LOGGED_IN_PATTERN.search(str(error_block or result_text)):
        raise CLINotLoggedInError(str(error_block or result_text))

    # Best-effort: Gemini CLI's documented `stats` block nests per-model
    # token counts (stats.models.<model>.tokens.{prompt,candidates,cached}).
    # Fall back to a flat "usage" shape (matching Claude Code's) if present,
    # else zero -- unverified against a live install either way.
    stats = payload.get("stats") or {}
    usage = payload.get("usage") or {}
    models_stats = stats.get("models") if isinstance(stats, dict) else None
    prompt_tokens = output_tokens = cached_tokens = 0
    if isinstance(models_stats, dict) and models_stats:
        first_model: dict[str, Any] = next(iter(models_stats.values()), {})
        tokens = first_model.get("tokens", {}) if isinstance(first_model, dict) else {}
        prompt_tokens = int(tokens.get("prompt", 0) or 0)
        output_tokens = int(tokens.get("candidates", 0) or 0)
        cached_tokens = int(tokens.get("cached", 0) or 0)
    elif usage:
        prompt_tokens = int(usage.get("input_tokens", 0) or 0)
        output_tokens = int(usage.get("output_tokens", 0) or 0)
        cached_tokens = int(usage.get("cache_read_input_tokens", 0) or 0)

    total_cost = payload.get("total_cost_usd")
    return CLIResult(
        result_text=result_text,
        is_error=is_error,
        subtype=str(payload.get("subtype", "")),
        num_turns=int(payload.get("num_turns", 0) or payload.get("turns", 0) or 0),
        session_id=payload.get("session_id"),
        input_tokens=prompt_tokens,
        output_tokens=output_tokens,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=cached_tokens,
        total_cost_usd=Decimal(str(total_cost)) if total_cost is not None else None,
        raw=payload,
    )


# Reuse agent_free.py's daily-CLI-call-count helpers unchanged - see
# agent_codex.py's identical comment for the rationale (same
# backend-agnostic `cli_invocation` audit_log action, and it lets
# `isolated_cli_call_date` isolate this backend's tests with no separate
# fixture needed).
_cli_calls_today = agent_free._cli_calls_today
_is_cli_budget_paused = agent_free._is_cli_budget_paused


async def _record_cli_invocation(
    session: Any, *, heal_job_id: int, cli_result: CLIResult | None, error: str | None
) -> None:
    details: dict[str, Any] = (
        {"error": error, "backend": "gemini_cli"} if error else {"backend": "gemini_cli"}
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


# Same reuse of agent_free.py's prompt builders/verification helper as
# agent_codex.py - see that module's comment for the rationale.
_runtime_prompt = agent_free._runtime_prompt
_ci_prompt = agent_free._ci_prompt
_verify_and_summarize = agent_free._verify_and_summarize
_rerun_workflow_called_since = agent_free._rerun_workflow_called_since


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


async def run_heal_job_gemini(
    job_id: int,
    *,
    mcp: MCPToolClient,
    github: GitHubClient,
    remote: str = "origin",
) -> None:
    """Gemini-CLI-backed equivalent of `healer.agent_free.run_heal_job_free`.

    Same interface and outer shape as `run_heal_job_codex` (see that
    function's docstring) — only the CLI call itself differs.
    """
    async with session_scope() as session:
        job = await session.get(HealJob, job_id)
        if job is None:
            logger.warning("agent_gemini.job_not_found", heal_job_id=job_id)
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
            f"run_heal_job_gemini only handles runtime_error/contract_violation, got {job_type}"
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
                cli_result = await run_gemini_cli(prompt, cwd=worktree_path)
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
                    reason=f"Gemini CLI is not logged in: {exc}",
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
                        details={"category": "gemini_usage_limit", "detail": str(exc)},
                    )
                return
            except _CLI_INFRA_ERRORS as exc:
                logger.error("agent_gemini.cli_call_failed", heal_job_id=job_id, error=str(exc))
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
                    job.pr_opened_at = datetime.now(UTC)
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


async def run_ci_heal_job_gemini(
    job_id: int,
    *,
    mcp: MCPToolClient,
    github: GitHubClient,
    remote: str = "origin",
) -> None:
    """Gemini-CLI-backed equivalent of `healer.agent_free.run_ci_heal_job_free`.

    Reuses `ci_agent._prepare_job`/`_open_needs_human_issue` unchanged, same
    as every other backend's CI-fix entry point.
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
            cli_result = await run_gemini_cli(prompt, cwd=worktree_path)
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
            root_cause = f"Gemini CLI is not logged in: {exc}"
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
                    details={"category": "gemini_usage_limit", "detail": str(exc)},
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
            logger.exception("agent_gemini.ci_pr_comment_failed", heal_job_id=job_id)

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
                logger.exception("agent_gemini.ci_needs_human_issue_failed", heal_job_id=job_id)
    finally:
        await remove_worktree(worktree_name, branch)


__all__ = [
    "CLIError",
    "CLIResult",
    "run_ci_heal_job_gemini",
    "run_gemini_cli",
    "run_heal_job_gemini",
]

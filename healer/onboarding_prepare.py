"""AI-driven onboarding: for a connected app that `selfheal prepare`
(`core/repo_health_check.py`) flags as missing infrastructure, offers ONE
PR (only after an operator confirmation in the CLI, which shows a rough
cost estimate first -- see `cli/main.py:prepare`) adding only what's
missing: starter characterization tests, a GitHub Actions test workflow
(with service containers for any detected external-service dependency),
and a `.env.test` file of FAKE placeholder credentials.

Reuses the exact same `propose_patch`/`run_tests` guardrails every other
fix in this project already gets, via a synthetic `HealJob` row
(`type=RUNTIME_ERROR`, no `source_error_id`/`source_contract_violation_id`
-- there's no bug here, just a gap) so write-scope enforcement needed zero
new code: `propose_patch` already derives its allowed prefixes from
`app.allowed_write_paths`, which is the whole point of reusing this
mechanism rather than inventing a parallel one.

**Verification**: a light-dependency app gets ONE local run of the new
tests through the real `run_tests` MCP tool -- if they don't pass on the
current code, no PR is opened at all (no fake onboarding, per the task's
own "must pass on the current code" requirement). A heavy-dependency app
(`core.scanner.detect_heavy_dependencies`) can't run anything locally, so
its onboarding PR is opened unverified-locally too, labeled the same
"verified by CI, not locally" way `healer/remote_verify.py` already
established, and never auto-merges either (same `auto_merge_override =
False` mechanism, same `healer.automerge.effective_auto_merge` precedence).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import structlog

from core.config import settings
from core.db import session_scope
from core.models import HealJob, HealJobStatus, HealJobType, MonitoredApp
from core.repo_health_check import PrepareReport, analyze_repo
from core.untrusted import UNTRUSTED_DATA_SYSTEM_PROMPT_NOTE
from healer import agent_free
from healer.github_ops import AUTO_FIX_LABEL, REMOTE_VERIFY_LABEL
from healer.mcp_client import MCPToolClient
from healer.worktree import (
    WorktreeError,
    commit_and_push,
    create_worktree_for_connected_app,
    remove_plain_clone,
)
from mcp_server import git_utils
from mcp_server.github_client import GitHubClient
from mcp_server.sandbox import REPO_ROOT

logger = structlog.get_logger(__name__)

ONBOARDING_LABEL = "selfheal-onboarding"

_SERVICE_ENV_VARS: dict[str, list[tuple[str, str]]] = {
    "postgresql": [("DATABASE_URL", "postgresql://test:test@localhost:5432/test")],
    "mysql": [("DATABASE_URL", "mysql://test:test@localhost:3306/test")],
    "redis": [("REDIS_URL", "redis://localhost:6379/0")],
    "mongodb": [("MONGO_URI", "mongodb://localhost:27017/test")],
}

_SERVICE_CONTAINERS: dict[str, str] = {
    "postgresql": "postgres:16",
    "mysql": "mysql:8",
    "redis": "redis:7",
    "mongodb": "mongo:7",
}


def estimate_onboarding_cost(report: PrepareReport) -> str:
    """A rough, honest ESTIMATE shown to the operator before they confirm --
    not real spend tracking (that depends on which AI backend is active and
    how large the app is); see `cli/main.py:prepare`'s confirmation prompt."""
    scope = len(report.missing)
    if scope <= 1:
        return "small (roughly one CLI turn, a few cents to ~$1 of AI usage)"
    if scope <= 3:
        return "moderate (a few CLI turns, roughly $1-$3 of AI usage)"
    return (
        "larger (several CLI turns, roughly $3-$6+ of AI usage -- tests, CI "
        "workflow, and test config are all missing)"
    )


def _onboarding_prompt(*, report: PrepareReport, worktree: str, heal_job_id: int) -> str:
    lines = [
        "You are an autonomous repo-onboarding agent for a self-healing application.",
        "",
        "This repo is missing infrastructure needed for automated bug-fixing. Add "
        "ONLY what's listed below, nothing else -- do not fix unrelated bugs.",
        f"heal_job_id to pass to propose_patch: {heal_job_id}.",
        f'Worktree name to pass as the "worktree" argument in tool calls: {worktree!r}.',
        "",
    ]
    step = 1
    if not report.has_tests:
        lines += [
            f"{step}. STARTER TESTS: write a small number of characterization tests for",
            "   the main modules/endpoints, capturing CURRENT behavior (not fixing bugs).",
            '   Mark each new test file clearly near the top: "starter tests: review '
            'before relying on them". They must pass against the CURRENT code.',
            "",
        ]
        step += 1
    if not report.has_ci_workflow or not report.ci_runs_tests:
        service_note = ""
        if report.external_services:
            containers = ", ".join(
                f"{s} ({_SERVICE_CONTAINERS.get(s, 'a matching image')})"
                for s in report.external_services
            )
            service_note = f" Use GitHub Actions service containers for: {containers}."
        lines += [
            f"{step}. CI WORKFLOW: add or extend a GitHub Actions workflow "
            "(.github/workflows/) that installs dependencies and runs the tests for "
            f"this project's language.{service_note}",
            "   It must run only on GitHub's own hosted runners (no self-hosted runner).",
            "",
        ]
        step += 1
    if report.external_services:
        placeholders = [
            f"{name}={value}"
            for service in report.external_services
            for name, value in _SERVICE_ENV_VARS.get(service, [])
        ]
        lines += [
            f"{step}. TEST CONFIG: add a .env.test file with FAKE placeholder values "
            "only (never real secrets), for example:",
            "   " + "\n   ".join(placeholders),
            "   Add mocks for any external API the new tests would otherwise call for real.",
            "",
        ]
    lines += [
        "Call propose_patch ONCE with a single diff containing everything above, then",
        "reply with a short final summary (what you added and why) and do not call any more tools.",
        "",
        "Hard rules, enforced by the surrounding system in code, not just by this prompt:",
        "- Patches are capped in size; keep changes minimal and targeted.",
        "- Never delete, skip, or weaken an existing test.",
        "- You may only modify files under this app's own directory.",
        f"- {UNTRUSTED_DATA_SYSTEM_PROMPT_NOTE}",
    ]
    return "\n".join(lines)


@dataclass(frozen=True)
class OnboardingResult:
    status: str  # "already_fixable" | "pr_opened" | "failed"
    detail: str
    pr_number: int | None = None
    pr_url: str | None = None


async def _finish_job(
    job_id: int,
    *,
    status: HealJobStatus,
    error: str | None = None,
    pr_number: int | None = None,
    branch: str | None = None,
) -> None:
    async with session_scope() as session:
        job = await session.get(HealJob, job_id)
        assert job is not None
        job.status = status
        job.error_message = error
        job.finished_at = datetime.now(UTC)
        if pr_number is not None:
            job.pr_number = pr_number
            job.pr_opened_at = datetime.now(UTC)
            job.branch_name = branch
            # Never auto-merge an onboarding PR (touches test/CI infra, always
            # needs a human's own review regardless of verification depth).
            job.auto_merge_override = False


def _onboarding_pr_body(
    *, report: PrepareReport, root_cause: str, diff_stat: str, verified_locally: bool
) -> str:
    verify_note = (
        "The new starter tests were run locally against the current code and passed."
        if verified_locally
        else (
            "> ⚠️ **Verified by CI, not locally.** This app's dependencies are too "
            "heavy to install locally, so this PR was never run locally -- your own "
            "GitHub Actions CI is the real proof. **This PR will never auto-merge.**"
        )
    )
    return (
        f"## Onboarding: making {report.app_name} self-healable\n\n"
        f"{verify_note}\n\n"
        f"## What was added\n{root_cause}\n\n"
        f"## Diff summary\n```\n{diff_stat}\n```\n\n"
        "_Opened automatically by the AI self-healing system's `selfheal prepare` "
        "onboarding flow. Starter tests are meant as a review-and-adjust starting "
        "point, not a guarantee of correctness._"
    )


async def _open_onboarding_pull_request(
    github: GitHubClient,
    *,
    branch: str,
    base: str,
    report: PrepareReport,
    root_cause: str,
    diff_stat: str,
    verified_locally: bool,
) -> dict[str, Any]:
    pr = await github.create_pull_request(
        title=f"Onboarding: add starter tests/CI/test-config for {report.app_name}",
        body=_onboarding_pr_body(
            report=report,
            root_cause=root_cause,
            diff_stat=diff_stat,
            verified_locally=verified_locally,
        ),
        head=branch,
        base=base,
    )
    labels = [AUTO_FIX_LABEL, ONBOARDING_LABEL]
    if not verified_locally:
        labels.append(REMOTE_VERIFY_LABEL)
    await github.add_labels(pr["number"], labels)
    return pr


async def get_prepare_report(app_id: int) -> PrepareReport | None:
    """Compute the same static checklist `GET /api/apps/{id}/prepare` uses,
    for an app that lives in `connected_apps/` -- returns None if the app
    doesn't exist or isn't a connected external app."""
    async with session_scope() as session:
        app = await session.get(MonitoredApp, app_id)
        if app is None or app.repo_url is None:
            return None
        app_name = app.name
        language = app.language
        local_repo_path = app.local_repo_path
    app_dir = REPO_ROOT / local_repo_path
    if not app_dir.is_dir():
        return None
    return analyze_repo(app_dir, app_name=app_name, language=language)


async def run_onboarding_prepare(
    app_id: int, *, mcp: MCPToolClient, github: GitHubClient
) -> OnboardingResult:
    async with session_scope() as session:
        app = await session.get(MonitoredApp, app_id)
        if app is None or app.repo_url is None:
            return OnboardingResult(status="failed", detail="not a connected external app")
        app_name = app.name
        app_github_repo = app.github_repo
        app_local_repo_path = app.local_repo_path
        app_language = app.language

    app_dir = REPO_ROOT / app_local_repo_path
    report = analyze_repo(app_dir, app_name=app_name, language=app_language)
    if report.already_fixable:
        return OnboardingResult(status="already_fixable", detail="Nothing missing.")

    heavy = bool(report.heavy_dependencies)

    async with session_scope() as session:
        if await agent_free._is_cli_budget_paused(session):
            return OnboardingResult(status="failed", detail="paused: daily CLI-call budget reached")
        job = HealJob(
            type=HealJobType.RUNTIME_ERROR,
            status=HealJobStatus.RUNNING,
            fingerprint=f"onboarding-{app_name}-{uuid.uuid4().hex[:12]}",
            app_id=app_id,
        )
        session.add(job)
        await session.flush()
        job_id = job.id

    worktree_name = f"onboard-{job_id}"
    branch = f"selfheal-onboarding/{app_name}-{job_id}"
    worktree_path = await create_worktree_for_connected_app(
        worktree_name, branch, source_dir=REPO_ROOT / app_local_repo_path
    )

    try:
        prompt = _onboarding_prompt(report=report, worktree=worktree_name, heal_job_id=job_id)
        try:
            cli_result = await agent_free.run_claude_cli(prompt, cwd=worktree_path)
        except agent_free.ClaudeCLIError as exc:
            async with session_scope() as session:
                await agent_free._record_cli_invocation(
                    session, heal_job_id=job_id, cli_result=None, error=str(exc)
                )
            await _finish_job(job_id, status=HealJobStatus.FAILED, error=str(exc))
            return OnboardingResult(status="failed", detail=str(exc))

        async with session_scope() as session:
            await agent_free._record_cli_invocation(
                session, heal_job_id=job_id, cli_result=cli_result, error=None
            )

        # `git diff --stat` alone misses a brand-new untracked file (`git
        # apply` leaves one untracked, not staged) -- exactly what onboarding
        # usually produces (new test files, a new CI workflow, a new
        # .env.test), so "did anything change" is checked via `git status
        # --porcelain` instead (same fix `healer/agent_free.py:
        # _touched_paths` already needed for the same reason). `diff_stat`
        # itself is kept for the PR body's display text.
        touched_paths = await agent_free._touched_paths(worktree_path)
        diff_stat = (await git_utils.diff_stat(cwd=worktree_path)).strip() or "\n".join(
            touched_paths
        )
        if not touched_paths or cli_result.is_error:
            reason = cli_result.result_text or "(no diff produced / CLI reported an error)"
            await _finish_job(job_id, status=HealJobStatus.FAILED, error=reason)
            return OnboardingResult(status="failed", detail=reason)

        if not heavy:
            test_result = await mcp.call_tool(
                "run_tests", {"worktree": worktree_name, "heal_job_id": job_id}
            )
            passed = bool(test_result.get("passed")) if isinstance(test_result, dict) else False
            if not passed:
                await _finish_job(
                    job_id, status=HealJobStatus.FAILED, error="starter tests failed locally"
                )
                return OnboardingResult(
                    status="failed",
                    detail="Generated starter tests failed locally -- no PR opened.",
                )

        push_remote = (
            f"https://x-access-token:{settings.github_token}@github.com/{app_github_repo}.git"
        )
        repo_info = await github.get_repo()
        base_branch = str(repo_info.get("default_branch") or "main")
        commit_msg = f"Onboarding: add starter tests/CI/test-config\n\nheal_job #{job_id}"
        try:
            await commit_and_push(worktree_path, branch, message=commit_msg, remote=push_remote)
        except WorktreeError as exc:
            # A real push failure (e.g. a token with insufficient repo
            # permission) used to propagate unhandled out of this function,
            # leaving the heal_job stuck in `running` forever -- never
            # `failed`, so it could never be retried or reported on. `exc`'s
            # message is already scrubbed of any credentialed URL by
            # `healer/worktree.py`, so it's safe to store as-is.
            await _finish_job(job_id, status=HealJobStatus.FAILED, error=str(exc))
            return OnboardingResult(status="failed", detail=str(exc))

        pr = await _open_onboarding_pull_request(
            github,
            branch=branch,
            base=base_branch,
            report=report,
            root_cause=cli_result.result_text or "(agent produced no final summary)",
            diff_stat=diff_stat,
            verified_locally=not heavy,
        )
        await _finish_job(
            job_id, status=HealJobStatus.PR_OPENED, pr_number=pr["number"], branch=branch
        )
        return OnboardingResult(
            status="pr_opened",
            detail="PR opened.",
            pr_number=pr["number"],
            pr_url=pr.get("html_url"),
        )
    finally:
        await remove_plain_clone(worktree_name)

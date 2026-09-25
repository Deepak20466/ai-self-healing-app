"""Opens the PR (on success) or a GitHub issue (on a low-confidence/exhausted
attempt) for a heal_job.

SPEC.md: on success, "push and open a PR whose body contains the root cause,
the diff summary, test evidence and the error link" and "label the PR
auto-fix"; "if Claude's confidence is low or tests cannot reproduce the bug,
open a GitHub issue with the analysis instead of a PR."
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from core.config import settings
from mcp_server.github_client import GitHubClient

AUTO_FIX_LABEL = "auto-fix"
NEEDS_REVIEW_LABEL = "needs-human-review"


@dataclass(frozen=True)
class CIFixOutcome:
    """Evidence for one CI-fix attempt's PR comment (`healer/ci_agent.py`)."""

    heal_job_id: int
    pr_number: int
    attempt_number: int
    max_attempts: int
    run_id: int
    outcome: str
    """One of "fixed", "flaky_rerun", "failed"."""
    root_cause: str
    diff_stat: str
    test_output: str


def _ci_fix_comment_body(evidence: CIFixOutcome) -> str:
    header = {
        "fixed": "✅ **Auto-fix pushed**",
        "flaky_rerun": "🔁 **Classified as flaky — re-ran the failed job**",
        "failed": "⚠️ **Auto-fix attempt did not succeed**",
    }[evidence.outcome]

    parts = [
        f"{header} (attempt {evidence.attempt_number}/{evidence.max_attempts}, "
        f"run {evidence.run_id})",
        "",
        f"## Root cause / analysis\n{evidence.root_cause}",
    ]
    if evidence.diff_stat:
        parts.append(f"## Diff summary\n```\n{evidence.diff_stat}\n```")
    if evidence.test_output:
        parts.append(f"## Test evidence\n```\n{evidence.test_output[-4000:]}\n```")
    parts.append(
        f"\n_Posted automatically by the AI self-healing system "
        f"(heal_job #{evidence.heal_job_id})._"
    )
    return "\n\n".join(parts)


async def post_ci_fix_comment(client: GitHubClient, evidence: CIFixOutcome) -> None:
    """Post the per-attempt PR comment SPEC.md requires: "After every attempt,
    post a PR comment with the root cause, the fix and the evidence."""
    await client.create_issue_comment(evidence.pr_number, _ci_fix_comment_body(evidence))


async def open_ci_needs_human_issue(
    client: GitHubClient, *, pr_number: int, attempts_summary: str
) -> dict[str, Any]:
    """Open an issue once the CI-fix circuit breaker trips for a PR
    (SPEC.md SAFETY GUARDRAILS: "max 2 CI-fix attempts per PR")."""
    body = (
        f"Automated CI healing could not resolve the failures on PR #{pr_number} "
        "within the allowed number of attempts.\n\n"
        f"## What was tried\n{attempts_summary}\n\n"
        "_Opened automatically by the AI self-healing system — human review needed._"
    )
    return await client.create_issue(
        title=f"Needs human review: CI failing on PR #{pr_number}",
        body=body,
        labels=[NEEDS_REVIEW_LABEL],
    )


@dataclass(frozen=True)
class FixEvidence:
    """Everything a PR/issue body needs, gathered by `runtime_agent.py`."""

    heal_job_id: int
    fingerprint: str
    root_cause: str
    diff_stat: str
    test_output: str
    error_summary: str


def _auto_merge_note() -> str:
    if settings.auto_merge:
        return "AUTO_MERGE is enabled — this PR merges automatically once CI passes."
    return "AUTO_MERGE is disabled — this PR waits for human approval."


def _pr_body(evidence: FixEvidence) -> str:
    return (
        f"## Root cause\n{evidence.root_cause}\n\n"
        f"## Diff summary\n```\n{evidence.diff_stat}\n```\n\n"
        f"## Test evidence\n```\n{evidence.test_output[-4000:]}\n```\n\n"
        f"## Source\nheal_job #{evidence.heal_job_id} "
        f"(fingerprint `{evidence.fingerprint}`): {evidence.error_summary}\n\n"
        f"_Opened automatically by the AI self-healing system. {_auto_merge_note()}_"
    )


async def open_fix_pull_request(
    client: GitHubClient, *, branch: str, base: str, evidence: FixEvidence
) -> dict[str, Any]:
    """Open the fix PR and label it `auto-fix`."""
    pr = await client.create_pull_request(
        title=f"Auto-fix: {evidence.error_summary}",
        body=_pr_body(evidence),
        head=branch,
        base=base,
    )
    await client.add_labels(pr["number"], [AUTO_FIX_LABEL])
    return pr


async def open_low_confidence_issue(
    client: GitHubClient, *, evidence: FixEvidence, attempts_summary: str
) -> dict[str, Any]:
    """Open an issue instead of a PR, per SPEC.md's low-confidence fallback."""
    body = (
        "Automated healing could not produce a verified fix.\n\n"
        f"## Source\nheal_job #{evidence.heal_job_id} "
        f"(fingerprint `{evidence.fingerprint}`): {evidence.error_summary}\n\n"
        f"## What was tried\n{attempts_summary}\n\n"
        "_Opened automatically by the AI self-healing system in place of a PR — "
        "confidence was low or the regression test could not be made to pass. "
        "Human review needed._"
    )
    return await client.create_issue(
        title=f"Needs human review: {evidence.error_summary}",
        body=body,
        labels=[NEEDS_REVIEW_LABEL],
    )

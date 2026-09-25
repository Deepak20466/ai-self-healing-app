"""scripts.ci_webhook_notify.build_payload: pure env -> JSON-body logic.

Regression coverage for a real bug found running the live CI self-healing
demo: GitHub Actions silently discards a step's attempt to override its own
reserved GITHUB_* env vars (GITHUB_RUN_ID, GITHUB_REF_NAME, GITHUB_SHA,
GITHUB_WORKFLOW) with the runner's real values for the CURRENTLY EXECUTING
workflow -- so ci-failure.yml's original custom `env:` block silently sent
its OWN run's branch ("main") and run id instead of the failed CI run's.
SOURCE_* (non-reserved names) is the fix; these tests pin that precedence.
"""

from __future__ import annotations

from scripts.ci_webhook_notify import build_payload


def test_source_vars_win_over_reserved_github_vars() -> None:
    env = {
        "SOURCE_RUN_ID": "111",
        "SOURCE_WORKFLOW": "CI",
        "SOURCE_BRANCH": "autofix-demo/ci-break-live",
        "SOURCE_SHA": "abc123",
        "PR_NUMBER": "14",
        # These simulate what the runner actually injects for ci-failure.yml's
        # OWN workflow_run-triggered run -- must be ignored in favor of SOURCE_*.
        "GITHUB_RUN_ID": "999999999",
        "GITHUB_WORKFLOW": "CI failure -> AI fix",
        "GITHUB_REF_NAME": "main",
        "GITHUB_SHA": "deadbeef",
    }
    payload = build_payload(env, status="finished", conclusion="failure")
    assert payload["run_id"] == 111
    assert payload["workflow"] == "CI"
    assert payload["branch"] == "autofix-demo/ci-break-live"
    assert payload["sha"] == "abc123"
    assert payload["pr_number"] == 14
    assert payload["conclusion"] == "failure"
    assert payload["status"] == "completed"


def test_falls_back_to_ambient_github_vars_when_no_source_vars_set() -> None:
    """ci.yml's own start/finish calls never set SOURCE_* -- they should use
    the ambient GITHUB_* vars, which correctly describe ci.yml's own run."""
    env = {
        "GITHUB_RUN_ID": "222",
        "GITHUB_WORKFLOW": "CI",
        "GITHUB_HEAD_REF": "feature/some-branch",
        "GITHUB_REF_NAME": "222/merge",
        "GITHUB_SHA": "cafebabe",
    }
    payload = build_payload(env, status="started", conclusion=None)
    assert payload["run_id"] == 222
    assert payload["workflow"] == "CI"
    assert payload["branch"] == "feature/some-branch"
    assert payload["sha"] == "cafebabe"
    assert payload["status"] == "in_progress"
    assert "pr_number" not in payload
    assert "conclusion" not in payload


def test_falls_back_to_ref_name_when_head_ref_is_empty() -> None:
    """A push-triggered run has no GITHUB_HEAD_REF (Actions sets it to "",
    not unset) -- must fall through to GITHUB_REF_NAME, not treat "" as real."""
    env = {"GITHUB_HEAD_REF": "", "GITHUB_REF_NAME": "main"}
    payload = build_payload(env, status="started", conclusion=None)
    assert payload["branch"] == "main"

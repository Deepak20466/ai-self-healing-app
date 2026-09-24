"""mcp_server.tools.cicd: GitHub Actions/PR tools, mocked via respx."""

from __future__ import annotations

import httpx
import pytest

from core.config import settings as core_settings
from mcp_server.confirmation import issue_confirmation_token
from mcp_server.github_client import GITHUB_API_BASE
from mcp_server.tools._exceptions import ToolError
from mcp_server.tools.cicd import (
    cancel_workflow,
    get_job_logs,
    get_pr_status,
    get_workflow_run,
    list_workflow_runs,
    rerun_workflow,
    trigger_rollback,
)

REPO = "acme/self-healing"


@pytest.fixture(autouse=True)
def _configure_github(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(core_settings, "github_token", "test-token")
    monkeypatch.setattr(core_settings, "github_repo", REPO)
    monkeypatch.setattr(core_settings, "session_secret", "test-session-secret")


async def test_list_workflow_runs_maps_fields(respx_mock) -> None:
    respx_mock.get(f"{GITHUB_API_BASE}/repos/{REPO}/actions/runs").mock(
        return_value=httpx.Response(
            200,
            json={
                "workflow_runs": [
                    {
                        "id": 111,
                        "name": "ci.yml",
                        "head_branch": "autofix/abc123",
                        "head_sha": "a" * 40,
                        "status": "completed",
                        "conclusion": "failure",
                        "html_url": "https://github.com/acme/self-healing/actions/runs/111",
                    }
                ]
            },
        )
    )

    runs = await list_workflow_runs(branch="autofix/abc123")
    assert runs == [
        {
            "run_id": 111,
            "workflow_name": "ci.yml",
            "branch": "autofix/abc123",
            "sha": "a" * 40,
            "status": "completed",
            "conclusion": "failure",
            "html_url": "https://github.com/acme/self-healing/actions/runs/111",
        }
    ]


async def test_get_workflow_run_returns_raw_payload(respx_mock) -> None:
    respx_mock.get(f"{GITHUB_API_BASE}/repos/{REPO}/actions/runs/111").mock(
        return_value=httpx.Response(200, json={"id": 111, "status": "completed"})
    )

    result = await get_workflow_run(111)
    assert result["id"] == 111


async def test_get_job_logs_trims_and_returns(respx_mock) -> None:
    respx_mock.get(f"{GITHUB_API_BASE}/repos/{REPO}/actions/runs/111/jobs").mock(
        return_value=httpx.Response(
            200,
            json={"jobs": [{"id": 555, "name": "pytest", "conclusion": "failure"}]},
        )
    )
    respx_mock.get(f"{GITHUB_API_BASE}/repos/{REPO}/actions/jobs/555/logs").mock(
        return_value=httpx.Response(
            200,
            text="##[group]Run pytest\nFAILED test\n##[error]exit 1\n##[endgroup]\n",
        )
    )

    result = await get_job_logs(111, "pytest")
    assert result["job_id"] == 555
    assert "FAILED test" in result["logs"]


async def test_get_job_logs_raises_for_unknown_job_name(respx_mock) -> None:
    respx_mock.get(f"{GITHUB_API_BASE}/repos/{REPO}/actions/runs/111/jobs").mock(
        return_value=httpx.Response(200, json={"jobs": [{"id": 1, "name": "lint"}]})
    )

    with pytest.raises(ToolError):
        await get_job_logs(111, "does-not-exist")


async def test_rerun_workflow_calls_failed_jobs_endpoint(respx_mock) -> None:
    route = respx_mock.post(
        f"{GITHUB_API_BASE}/repos/{REPO}/actions/runs/111/rerun-failed-jobs"
    ).mock(return_value=httpx.Response(201))

    result = await rerun_workflow(111, failed_only=True)
    assert route.called
    assert result["rerun"] is True


async def test_cancel_workflow_requires_confirmation_token() -> None:
    with pytest.raises(ToolError):
        await cancel_workflow(111, confirmation_token="")


async def test_cancel_workflow_rejects_wrong_action_token() -> None:
    token = issue_confirmation_token("trigger_rollback")  # wrong action
    with pytest.raises(ToolError):
        await cancel_workflow(111, confirmation_token=token)


async def test_cancel_workflow_succeeds_with_valid_token(respx_mock) -> None:
    route = respx_mock.post(f"{GITHUB_API_BASE}/repos/{REPO}/actions/runs/111/cancel").mock(
        return_value=httpx.Response(202)
    )
    token = issue_confirmation_token("cancel_workflow")

    result = await cancel_workflow(111, confirmation_token=token)
    assert route.called
    assert result["cancelled"] is True


async def test_get_pr_status_combines_reviews_and_checks(respx_mock) -> None:
    respx_mock.get(f"{GITHUB_API_BASE}/repos/{REPO}/pulls/12").mock(
        return_value=httpx.Response(
            200,
            json={
                "state": "open",
                "mergeable": True,
                "mergeable_state": "clean",
                "head": {"sha": "b" * 40},
            },
        )
    )
    respx_mock.get(f"{GITHUB_API_BASE}/repos/{REPO}/pulls/12/reviews").mock(
        return_value=httpx.Response(
            200, json=[{"user": {"login": "reviewer1"}, "state": "APPROVED"}]
        )
    )
    respx_mock.get(f"{GITHUB_API_BASE}/repos/{REPO}/commits/{'b' * 40}/check-runs").mock(
        return_value=httpx.Response(
            200,
            json={"check_runs": [{"name": "ci", "status": "completed", "conclusion": "success"}]},
        )
    )

    result = await get_pr_status(12)
    assert result["mergeable"] is True
    assert result["reviews"] == {"reviewer1": "APPROVED"}
    assert result["checks"][0]["conclusion"] == "success"


async def test_trigger_rollback_requires_confirmation_token() -> None:
    with pytest.raises(ToolError):
        await trigger_rollback("production", confirmation_token="")


async def test_trigger_rollback_dispatches_workflow_with_valid_token(respx_mock) -> None:
    route = respx_mock.post(
        f"{GITHUB_API_BASE}/repos/{REPO}/actions/workflows/rollback.yml/dispatches"
    ).mock(return_value=httpx.Response(204))
    token = issue_confirmation_token("trigger_rollback")

    result = await trigger_rollback("production", confirmation_token=token)
    assert route.called
    assert result["rollback_dispatched"] is True

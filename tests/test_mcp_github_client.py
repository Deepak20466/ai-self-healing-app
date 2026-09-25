"""mcp_server.github_client: config guards and non-2xx handling."""

from __future__ import annotations

import json

import httpx
import pytest

from core.config import settings as core_settings
from mcp_server.github_client import GITHUB_API_BASE, GitHubClient, GitHubClientError


async def test_missing_github_token_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(core_settings, "github_token", None)
    monkeypatch.setattr(core_settings, "github_repo", "acme/repo")

    async with GitHubClient() as client:
        with pytest.raises(GitHubClientError, match="GITHUB_TOKEN"):
            await client.list_workflow_runs()


async def test_missing_github_repo_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(core_settings, "github_token", "test-token")
    monkeypatch.setattr(core_settings, "github_repo", None)

    async with GitHubClient() as client:
        with pytest.raises(GitHubClientError, match="GITHUB_REPO"):
            await client.list_workflow_runs()


async def test_non_2xx_response_raises_with_status_and_body(
    monkeypatch: pytest.MonkeyPatch, respx_mock
) -> None:
    monkeypatch.setattr(core_settings, "github_token", "test-token")
    monkeypatch.setattr(core_settings, "github_repo", "acme/repo")

    respx_mock.get(f"{GITHUB_API_BASE}/repos/acme/repo/actions/runs/1").mock(
        return_value=httpx.Response(404, text="Not Found")
    )

    async with GitHubClient() as client:
        with pytest.raises(GitHubClientError, match="404"):
            await client.get_workflow_run(1)


async def test_request_includes_bearer_token_header(
    monkeypatch: pytest.MonkeyPatch, respx_mock
) -> None:
    monkeypatch.setattr(core_settings, "github_token", "my-secret-token")
    monkeypatch.setattr(core_settings, "github_repo", "acme/repo")

    route = respx_mock.get(f"{GITHUB_API_BASE}/repos/acme/repo/actions/runs/1").mock(
        return_value=httpx.Response(200, json={"id": 1})
    )

    async with GitHubClient() as client:
        await client.get_workflow_run(1)

    assert route.calls.last.request.headers["Authorization"] == "Bearer my-secret-token"


async def test_create_pull_request_posts_title_body_head_base(
    monkeypatch: pytest.MonkeyPatch, respx_mock
) -> None:
    monkeypatch.setattr(core_settings, "github_token", "test-token")
    monkeypatch.setattr(core_settings, "github_repo", "acme/repo")

    route = respx_mock.post(f"{GITHUB_API_BASE}/repos/acme/repo/pulls").mock(
        return_value=httpx.Response(201, json={"number": 7, "html_url": "https://example/pr/7"})
    )

    async with GitHubClient() as client:
        pr = await client.create_pull_request(
            title="Auto-fix: ZeroDivisionError",
            body="root cause...",
            head="autofix/abc-1",
            base="main",
        )

    assert pr["number"] == 7
    payload = json.loads(route.calls.last.request.content)
    assert payload == {
        "title": "Auto-fix: ZeroDivisionError",
        "body": "root cause...",
        "head": "autofix/abc-1",
        "base": "main",
    }


async def test_add_labels_posts_to_issue_labels_endpoint(
    monkeypatch: pytest.MonkeyPatch, respx_mock
) -> None:
    monkeypatch.setattr(core_settings, "github_token", "test-token")
    monkeypatch.setattr(core_settings, "github_repo", "acme/repo")

    route = respx_mock.post(f"{GITHUB_API_BASE}/repos/acme/repo/issues/7/labels").mock(
        return_value=httpx.Response(200, json=[])
    )

    async with GitHubClient() as client:
        await client.add_labels(7, ["auto-fix"])

    assert json.loads(route.calls.last.request.content) == {"labels": ["auto-fix"]}


async def test_create_issue_posts_title_body_and_labels(
    monkeypatch: pytest.MonkeyPatch, respx_mock
) -> None:
    monkeypatch.setattr(core_settings, "github_token", "test-token")
    monkeypatch.setattr(core_settings, "github_repo", "acme/repo")

    route = respx_mock.post(f"{GITHUB_API_BASE}/repos/acme/repo/issues").mock(
        return_value=httpx.Response(201, json={"number": 9})
    )

    async with GitHubClient() as client:
        issue = await client.create_issue(
            title="Needs human review: KeyError", body="details...", labels=["needs-human-review"]
        )

    assert issue["number"] == 9
    assert json.loads(route.calls.last.request.content) == {
        "title": "Needs human review: KeyError",
        "body": "details...",
        "labels": ["needs-human-review"],
    }


async def test_create_issue_comment_posts_body(monkeypatch: pytest.MonkeyPatch, respx_mock) -> None:
    monkeypatch.setattr(core_settings, "github_token", "test-token")
    monkeypatch.setattr(core_settings, "github_repo", "acme/repo")

    route = respx_mock.post(f"{GITHUB_API_BASE}/repos/acme/repo/issues/12/comments").mock(
        return_value=httpx.Response(201, json={"id": 1})
    )

    async with GitHubClient() as client:
        await client.create_issue_comment(12, "CI is now green.")

    assert json.loads(route.calls.last.request.content) == {"body": "CI is now green."}

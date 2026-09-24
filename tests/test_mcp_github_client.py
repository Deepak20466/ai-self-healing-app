"""mcp_server.github_client: config guards and non-2xx handling."""

from __future__ import annotations

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

"""Thin GitHub REST API client (tech stack: httpx, token from `GITHUB_TOKEN`).

Used by `tools/cicd.py`. Tests mock this with `respx` rather than hitting
the real API.
"""

from __future__ import annotations

from typing import Any

import httpx

from core.config import settings

GITHUB_API_BASE = "https://api.github.com"


class GitHubClientError(Exception):
    """Raised for a missing configuration value or a non-2xx GitHub response."""


def _headers() -> dict[str, str]:
    if not settings.github_token:
        raise GitHubClientError("GITHUB_TOKEN is not configured")
    return {
        "Authorization": f"Bearer {settings.github_token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


class GitHubClient:
    """`repo` overrides `GITHUB_REPO` for this instance (`owner/repo`) — used for
    connect-a-repo (multi-app) flows where each `MonitoredApp` may target a
    different GitHub repo than the default `settings.github_repo`. Omitted,
    this behaves exactly as before (single hardcoded repo from `.env`).
    """

    def __init__(self, client: httpx.AsyncClient | None = None, *, repo: str | None = None) -> None:
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(base_url=GITHUB_API_BASE, timeout=15.0)
        self._repo_override = repo

    def _repo_or_raise(self) -> str:
        repo = self._repo_override or settings.github_repo
        if not repo:
            raise GitHubClientError("GITHUB_REPO is not configured")
        return repo

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> GitHubClient:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        response = await self._client.request(method, path, headers=_headers(), **kwargs)
        if response.status_code >= 400:
            raise GitHubClientError(
                f"GitHub API {method} {path} failed: {response.status_code} {response.text[:500]}"
            )
        return response

    async def get_repo(self, repo: str | None = None) -> dict[str, Any]:
        """Fetch repo metadata — used by the "connect a repo" flow to confirm
        `GITHUB_TOKEN` can actually see this repo before cloning/registering it."""
        target = repo or self._repo_or_raise()
        response = await self._request("GET", f"/repos/{target}")
        data: dict[str, Any] = response.json()
        return data

    async def list_workflow_runs(
        self, *, branch: str | None = None, status: str | None = None
    ) -> list[dict[str, Any]]:
        repo = self._repo_or_raise()
        params: dict[str, str] = {}
        if branch:
            params["branch"] = branch
        if status:
            params["status"] = status
        response = await self._request("GET", f"/repos/{repo}/actions/runs", params=params)
        runs: list[dict[str, Any]] = response.json().get("workflow_runs", [])
        return runs

    async def get_workflow_run(self, run_id: int) -> dict[str, Any]:
        repo = self._repo_or_raise()
        response = await self._request("GET", f"/repos/{repo}/actions/runs/{run_id}")
        data: dict[str, Any] = response.json()
        return data

    async def list_jobs_for_run(self, run_id: int) -> list[dict[str, Any]]:
        repo = self._repo_or_raise()
        response = await self._request("GET", f"/repos/{repo}/actions/runs/{run_id}/jobs")
        jobs: list[dict[str, Any]] = response.json().get("jobs", [])
        return jobs

    async def get_job_logs_text(self, job_id: int) -> str:
        repo = self._repo_or_raise()
        response = await self._request("GET", f"/repos/{repo}/actions/jobs/{job_id}/logs")
        return response.text

    async def rerun_workflow(self, run_id: int, *, failed_only: bool = True) -> None:
        repo = self._repo_or_raise()
        suffix = "rerun-failed-jobs" if failed_only else "rerun"
        await self._request("POST", f"/repos/{repo}/actions/runs/{run_id}/{suffix}")

    async def cancel_workflow(self, run_id: int) -> None:
        repo = self._repo_or_raise()
        await self._request("POST", f"/repos/{repo}/actions/runs/{run_id}/cancel")

    async def get_pull_request(self, pr_number: int) -> dict[str, Any]:
        repo = self._repo_or_raise()
        response = await self._request("GET", f"/repos/{repo}/pulls/{pr_number}")
        data: dict[str, Any] = response.json()
        return data

    async def list_pull_request_reviews(self, pr_number: int) -> list[dict[str, Any]]:
        repo = self._repo_or_raise()
        response = await self._request("GET", f"/repos/{repo}/pulls/{pr_number}/reviews")
        reviews: list[dict[str, Any]] = response.json()
        return reviews

    async def list_check_runs(self, sha: str) -> list[dict[str, Any]]:
        repo = self._repo_or_raise()
        response = await self._request("GET", f"/repos/{repo}/commits/{sha}/check-runs")
        runs: list[dict[str, Any]] = response.json().get("check_runs", [])
        return runs

    async def dispatch_workflow(
        self, workflow_file: str, *, ref: str, inputs: dict[str, str] | None = None
    ) -> None:
        repo = self._repo_or_raise()
        payload: dict[str, Any] = {"ref": ref}
        if inputs:
            payload["inputs"] = inputs
        await self._request(
            "POST", f"/repos/{repo}/actions/workflows/{workflow_file}/dispatches", json=payload
        )

    async def create_pull_request(
        self, *, title: str, body: str, head: str, base: str = "main"
    ) -> dict[str, Any]:
        """Open a PR from `head` into `base`. Used by the healer, never surfaced as an MCP tool.

        Not in SPEC.md's mcp-pod tool list — SPEC.md's healer-pod bullet has
        the healer itself "commit ... push and open a PR", so this is called
        directly from `healer/github_ops.py`, the same way `tools/cicd.py`
        already imports `GitHubClient` directly rather than going through MCP.
        """
        repo = self._repo_or_raise()
        response = await self._request(
            "POST",
            f"/repos/{repo}/pulls",
            json={"title": title, "body": body, "head": head, "base": base},
        )
        data: dict[str, Any] = response.json()
        return data

    async def add_labels(self, issue_or_pr_number: int, labels: list[str]) -> None:
        """Add labels to a PR or issue (PRs are issues for labeling purposes)."""
        repo = self._repo_or_raise()
        await self._request(
            "POST",
            f"/repos/{repo}/issues/{issue_or_pr_number}/labels",
            json={"labels": labels},
        )

    async def create_issue(
        self, *, title: str, body: str, labels: list[str] | None = None
    ) -> dict[str, Any]:
        """Open an issue — SPEC.md's "low confidence" fallback when a heal attempt
        can't produce a verified fix (used instead of opening a PR)."""
        repo = self._repo_or_raise()
        payload: dict[str, Any] = {"title": title, "body": body}
        if labels:
            payload["labels"] = labels
        response = await self._request("POST", f"/repos/{repo}/issues", json=payload)
        data: dict[str, Any] = response.json()
        return data

    async def create_issue_comment(self, issue_or_pr_number: int, body: str) -> None:
        """Post a comment on an issue or PR (Phase 5 posts CI-fix evidence here)."""
        repo = self._repo_or_raise()
        await self._request(
            "POST", f"/repos/{repo}/issues/{issue_or_pr_number}/comments", json={"body": body}
        )

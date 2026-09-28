"""Terminal-only v1.0 Step 3: per-app auto-merge.

A heal_job's PR merges automatically only when ALL of these hold:
  1. its effective auto-merge decision (see `effective_auto_merge`) is True;
  2. the local full test suite already passed (a precondition for the PR
     existing at all -- see `healer/full_suite.py`, already enforced before
     any backend opens a PR);
  3. GitHub CI on the PR's head commit has ALSO passed for real (checked
     here, independently, never assumed from #2 -- a real CI failure a
     local run can't reproduce must still block a merge); and
  4. GitHub reports the PR as cleanly mergeable (no conflicts).

This runs as one background polling loop (same shape as sentinel-pod's
prober/anomaly loops), not per-backend logic -- a heal_job's PR looks
identical regardless of which of the 5 AI backends opened it.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import structlog
from sqlalchemy import select

from core.config import settings
from core.db import session_scope
from core.models import HealJob, HealJobStatus, MonitoredApp
from mcp_server.github_client import GitHubClient, GitHubClientError

logger = structlog.get_logger(__name__)

_TERMINAL_CHECK_CONCLUSIONS = {"success", "neutral", "skipped"}


def effective_auto_merge(job: HealJob, app: MonitoredApp | None) -> bool:
    """NULL override -> the app's own setting -> the global fallback."""
    if job.auto_merge_override is not None:
        return job.auto_merge_override
    if app is not None:
        return app.auto_merge
    return settings.auto_merge


async def _ci_has_passed(client: GitHubClient, sha: str) -> bool:
    runs = await client.list_check_runs(sha)
    if not runs:
        return False
    for run in runs:
        if run.get("status") != "completed":
            return False
        if run.get("conclusion") not in _TERMINAL_CHECK_CONCLUSIONS:
            return False
    return True


async def _try_merge_one(job_id: int, pr_number: int, repo: str | None) -> bool:
    """Returns True if it merged the PR. Never raises -- a transient GitHub
    error just means "try again next poll", not a crash of the loop."""
    try:
        async with GitHubClient(repo=repo) as client:
            pr = await client.get_pull_request(pr_number)
            if pr.get("merged"):
                return False
            if pr.get("draft"):
                return False
            if pr.get("mergeable_state") != "clean":
                return False
            sha = pr.get("head", {}).get("sha")
            if not sha or not await _ci_has_passed(client, sha):
                return False
            await client.merge_pull_request(pr_number)
    except GitHubClientError:
        logger.warning("automerge.github_error", job_id=job_id, pr_number=pr_number)
        return False
    async with session_scope() as session:
        row = await session.get(HealJob, job_id)
        if row is not None and row.status == HealJobStatus.PR_OPENED:
            row.status = HealJobStatus.MERGED
    logger.info("automerge.merged", job_id=job_id, pr_number=pr_number)
    return True


async def check_and_merge_eligible_jobs() -> int:
    """One pass over every open-PR heal_job whose effective auto-merge is on.
    Returns how many it merged."""
    async with session_scope() as session:
        stmt = select(HealJob).where(
            HealJob.status == HealJobStatus.PR_OPENED, HealJob.pr_number.is_not(None)
        )
        jobs = list((await session.execute(stmt)).scalars().all())
        apps: dict[int, MonitoredApp | None] = {}
        candidates: list[tuple[int, int, str | None]] = []
        for job in jobs:
            app = None
            if job.app_id is not None:
                if job.app_id not in apps:
                    apps[job.app_id] = await session.get(MonitoredApp, job.app_id)
                app = apps[job.app_id]
            if effective_auto_merge(job, app):
                assert job.pr_number is not None
                repo = app.github_repo if app is not None else None
                candidates.append((job.id, job.pr_number, repo))

    merged = 0
    for job_id, pr_number, repo in candidates:
        if await _try_merge_one(job_id, pr_number, repo):
            merged += 1
    return merged


async def run_auto_merge_loop(
    sleep: Callable[[float], Awaitable[None]] | None = None,
) -> None:
    """Background task, started alongside the worker loop in healer/app.py."""
    import asyncio

    _sleep = sleep or asyncio.sleep
    while True:
        try:
            await check_and_merge_eligible_jobs()
        except Exception:
            logger.exception("automerge.loop_iteration_failed")
        await _sleep(settings.auto_merge_poll_seconds)

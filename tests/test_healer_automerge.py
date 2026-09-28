"""healer/automerge.py: the terminal-only v1.0 Step 3 poller. GitHub is
mocked via respx (never a real merge); the DB side uses `core.db.
session_scope()` with randomized fingerprints, same tradeoff already
accepted for other session_scope()-based tests (see CLAUDE.md).
"""

from __future__ import annotations

import uuid
from typing import Any

import httpx
import pytest

from core.config import settings as core_settings
from core.db import session_scope
from core.models import HealJob, HealJobStatus, HealJobType, MonitoredApp
from healer.automerge import check_and_merge_eligible_jobs, effective_auto_merge

GITHUB_API_BASE = "https://api.github.com"


def _random_pr_number() -> int:
    return uuid.uuid4().int % 900_000 + 100_000


@pytest.fixture(autouse=True)
def _github_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(core_settings, "github_token", "gh-fake-token")


@pytest.fixture(autouse=True)
async def _cleanup_created_jobs() -> Any:
    """`check_and_merge_eligible_jobs` deliberately queries ALL heal_jobs with
    status=pr_opened, globally -- so a job this test creates but leaves
    un-merged would otherwise still be sitting there (real committed rows,
    session_scope() is never rolled back) for the NEXT test's own call to
    pick up and try to merge, hitting GitHub routes that test never mocked.
    Same "shared DB, unscoped query" family CLAUDE.md already documents for
    other session_scope()-based tests -- mark every job this test created
    as terminal (failed) once the test is done, so it can never bleed into
    another test's run of the same global query.
    """
    created: list[int] = []
    yield created
    if created:
        async with session_scope() as session:
            for job_id in created:
                row = await session.get(HealJob, job_id)
                if row is not None and row.status == HealJobStatus.PR_OPENED:
                    row.status = HealJobStatus.FAILED


async def _make_app(*, auto_merge: bool, github_repo: str = "acme/testapp") -> MonitoredApp:
    async with session_scope() as session:
        app_row = MonitoredApp(
            name=f"app-{uuid.uuid4().hex[:8]}",
            language="python",
            local_repo_path="connected_apps/testapp",
            github_repo=github_repo,
            allowed_write_paths=["connected_apps/testapp/"],
            test_command="pytest",
            ingest_token=uuid.uuid4().hex,
            auto_merge=auto_merge,
        )
        session.add(app_row)
        await session.flush()
        await session.refresh(app_row)
        session.expunge(app_row)
    return app_row


async def _make_job(
    tracker: list[int],
    *,
    app_id: int | None,
    pr_number: int,
    auto_merge_override: bool | None = None,
) -> HealJob:
    async with session_scope() as session:
        job = HealJob(
            type=HealJobType.RUNTIME_ERROR,
            status=HealJobStatus.PR_OPENED,
            fingerprint=uuid.uuid4().hex,
            pr_number=pr_number,
            app_id=app_id,
            auto_merge_override=auto_merge_override,
        )
        session.add(job)
        await session.flush()
        await session.refresh(job)
        session.expunge(job)
    tracker.append(job.id)
    return job


def test_effective_auto_merge_precedence() -> None:
    app_on = MonitoredApp(
        name="a",
        language="python",
        local_repo_path="x",
        github_repo="a/b",
        allowed_write_paths=["x/"],
        test_command="pytest",
        ingest_token="t",
        auto_merge=True,
    )
    app_off = MonitoredApp(
        name="b",
        language="python",
        local_repo_path="x",
        github_repo="a/b",
        allowed_write_paths=["x/"],
        test_command="pytest",
        ingest_token="t2",
        auto_merge=False,
    )
    job_no_override = HealJob(
        type=HealJobType.RUNTIME_ERROR,
        fingerprint="f1",
        auto_merge_override=None,
    )
    job_override_true = HealJob(
        type=HealJobType.RUNTIME_ERROR, fingerprint="f2", auto_merge_override=True
    )
    job_override_false = HealJob(
        type=HealJobType.RUNTIME_ERROR, fingerprint="f3", auto_merge_override=False
    )

    assert effective_auto_merge(job_no_override, app_on) is True
    assert effective_auto_merge(job_no_override, app_off) is False
    assert effective_auto_merge(job_override_true, app_off) is True
    assert effective_auto_merge(job_override_false, app_on) is False
    assert effective_auto_merge(job_no_override, None) is False


async def test_merges_when_ci_and_mergeable_state_are_clean(
    respx_mock: Any, _cleanup_created_jobs: list[int]
) -> None:
    app_row = await _make_app(auto_merge=True)
    pr_number = _random_pr_number()
    job = await _make_job(_cleanup_created_jobs, app_id=app_row.id, pr_number=pr_number)

    respx_mock.get(f"{GITHUB_API_BASE}/repos/acme/testapp/pulls/{pr_number}").mock(
        return_value=httpx.Response(
            200,
            json={
                "merged": False,
                "draft": False,
                "mergeable_state": "clean",
                "head": {"sha": "abc"},
            },
        )
    )
    respx_mock.get(f"{GITHUB_API_BASE}/repos/acme/testapp/commits/abc/check-runs").mock(
        return_value=httpx.Response(
            200, json={"check_runs": [{"status": "completed", "conclusion": "success"}]}
        )
    )
    merge_route = respx_mock.put(
        f"{GITHUB_API_BASE}/repos/acme/testapp/pulls/{pr_number}/merge"
    ).mock(return_value=httpx.Response(200, json={"merged": True}))

    merged = await check_and_merge_eligible_jobs()
    assert merged == 1
    assert merge_route.called

    async with session_scope() as session:
        refreshed = await session.get(HealJob, job.id)
        assert refreshed is not None
        assert refreshed.status == HealJobStatus.MERGED


async def test_does_not_merge_when_app_auto_merge_is_off(
    respx_mock: Any, _cleanup_created_jobs: list[int]
) -> None:
    app_row = await _make_app(auto_merge=False)
    await _make_job(_cleanup_created_jobs, app_id=app_row.id, pr_number=_random_pr_number())

    merged = await check_and_merge_eligible_jobs()
    assert merged == 0
    assert not respx_mock.calls


async def test_per_fix_override_wins_over_app_setting(
    respx_mock: Any, _cleanup_created_jobs: list[int]
) -> None:
    app_row = await _make_app(auto_merge=True)
    await _make_job(
        _cleanup_created_jobs,
        app_id=app_row.id,
        pr_number=_random_pr_number(),
        auto_merge_override=False,
    )

    merged = await check_and_merge_eligible_jobs()
    assert merged == 0
    assert not respx_mock.calls


async def test_does_not_merge_when_ci_still_running(
    respx_mock: Any, _cleanup_created_jobs: list[int]
) -> None:
    app_row = await _make_app(auto_merge=True)
    pr_number = _random_pr_number()
    await _make_job(_cleanup_created_jobs, app_id=app_row.id, pr_number=pr_number)

    respx_mock.get(f"{GITHUB_API_BASE}/repos/acme/testapp/pulls/{pr_number}").mock(
        return_value=httpx.Response(
            200,
            json={
                "merged": False,
                "draft": False,
                "mergeable_state": "clean",
                "head": {"sha": "def"},
            },
        )
    )
    respx_mock.get(f"{GITHUB_API_BASE}/repos/acme/testapp/commits/def/check-runs").mock(
        return_value=httpx.Response(
            200, json={"check_runs": [{"status": "in_progress", "conclusion": None}]}
        )
    )

    merged = await check_and_merge_eligible_jobs()
    assert merged == 0


async def test_does_not_merge_a_conflicted_pr(
    respx_mock: Any, _cleanup_created_jobs: list[int]
) -> None:
    app_row = await _make_app(auto_merge=True)
    pr_number = _random_pr_number()
    await _make_job(_cleanup_created_jobs, app_id=app_row.id, pr_number=pr_number)

    respx_mock.get(f"{GITHUB_API_BASE}/repos/acme/testapp/pulls/{pr_number}").mock(
        return_value=httpx.Response(
            200, json={"merged": False, "draft": False, "mergeable_state": "dirty"}
        )
    )

    merged = await check_and_merge_eligible_jobs()
    assert merged == 0


async def test_global_fallback_used_when_job_has_no_app(
    respx_mock: Any, _cleanup_created_jobs: list[int]
) -> None:
    """A job with no app_id (e.g. this project's own pre-multi-app rows) falls
    back to the global AUTO_MERGE setting, not an app row."""
    await _make_job(_cleanup_created_jobs, app_id=None, pr_number=_random_pr_number())

    merged = await check_and_merge_eligible_jobs()
    assert merged == 0
    assert not respx_mock.calls

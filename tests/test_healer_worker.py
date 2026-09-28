"""healer.worker._process_next_job: the global-hourly-cap requeue path.

Regression coverage for a real bug found running the live CI self-healing
demo: hitting the global hourly cap requeued the dequeued job and returned
True ("a job was claimed, try again immediately") -- but the main loop in
`run_worker` treats True as "don't wait, loop right back into
_process_next_job". Since `dequeue_heal_job` is FIFO, the SAME just-requeued
job is immediately dequeued again, hits the cap again, forever -- a 100%-CPU
busy-spin that starves every other queued job (including a genuinely new one
enqueued behind it) for as long as the cap stays open, which can be most of
an hour. Fixed to return False, so the main loop backs off via its
notify/fallback wait instead of spinning.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

import pytest

from core.config import settings as core_settings
from core.db import session_scope
from core.models import HealJob, HealJobStatus, HealJobType, MonitoredApp
from core.queue import dequeue_heal_job, enqueue_heal_job
from healer import worker as worker_module


@pytest.mark.asyncio
async def test_hitting_the_global_cap_requeues_and_backs_off_instead_of_spinning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fingerprint = f"test-cap-{uuid.uuid4().hex}"
    async with session_scope() as session:
        job = await enqueue_heal_job(
            session, type=HealJobType.RUNTIME_ERROR, fingerprint=fingerprint
        )
        job_id = job.id

    async def _cap_always_open(session: object, *, max_per_hour: int) -> bool:
        return True

    monkeypatch.setattr(worker_module, "global_hourly_circuit_open", _cap_always_open)

    claimed = await worker_module._process_next_job(mcp=None, backend=None)  # type: ignore[arg-type]

    # False, not True: the main loop must NOT immediately retry (that's what
    # caused the busy-spin/starvation bug) -- it should back off and wait.
    assert claimed is False

    async with session_scope() as session:
        refreshed = await session.get(HealJob, job_id)
        assert refreshed is not None
        assert refreshed.status == HealJobStatus.QUEUED
        assert refreshed.started_at is None
        # Cleanup: a job left QUEUED forever would otherwise sit at the front
        # of the shared test DB's FIFO queue and get dequeued by every later
        # test in this file (or a later run) instead of that test's own job
        # -- reproduced for real while adding the test below.
        refreshed.status = HealJobStatus.FAILED


@dataclass
class _FakeGitHubClient:
    repo: str | None
    calls: list[str] = field(default_factory=list)

    async def __aenter__(self) -> _FakeGitHubClient:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        return None


async def test_process_next_job_uses_the_jobs_own_app_github_repo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A multi-app heal_job must open its PR/issue against its own app's
    `github_repo` (a connect-a-repo app's own GitHub repo), not the global
    `GITHUB_REPO` setting — this is what lets the healer push fixes to a
    connected app's real repo instead of this project's own."""
    # Defensively drain any stale QUEUED rows left behind by an earlier test
    # run in this shared test DB -- dequeue_heal_job is FIFO, so a leftover
    # row would otherwise be claimed instead of the job this test enqueues
    # below, and this test would spuriously see app_id=None / repo=None.
    async with session_scope() as session:
        while True:
            stale = await dequeue_heal_job(
                session,
                types=(
                    HealJobType.RUNTIME_ERROR,
                    HealJobType.CONTRACT_VIOLATION,
                    HealJobType.CI_FAILURE,
                ),
            )
            if stale is None:
                break
            stale.status = HealJobStatus.FAILED

    fingerprint = f"test-app-repo-{uuid.uuid4().hex}"
    async with session_scope() as session:
        app = MonitoredApp(
            name=f"conn-{uuid.uuid4().hex[:8]}",
            language="python",
            local_repo_path=f"connected_apps/conn-{uuid.uuid4().hex[:8]}",
            github_repo="someone/their-repo",
            allowed_write_paths=["connected_apps/x/"],
            test_command="pytest",
            ingest_token=uuid.uuid4().hex,
        )
        session.add(app)
        await session.flush()
        app_id = app.id
        job = await enqueue_heal_job(
            session,
            type=HealJobType.RUNTIME_ERROR,
            fingerprint=fingerprint,
            app_id=app_id,
        )
        job_id = job.id

    created_clients: list[_FakeGitHubClient] = []

    def _fake_github_client(*, repo: str | None = None) -> _FakeGitHubClient:
        client = _FakeGitHubClient(repo=repo)
        created_clients.append(client)
        return client

    async def _runtime_runner(job_id: int, *, mcp: Any, github: Any) -> None:
        return None

    async def _cap_never_open(session: object, *, max_per_hour: int) -> bool:
        return False

    monkeypatch.setattr(worker_module, "GitHubClient", _fake_github_client)
    monkeypatch.setattr(worker_module, "global_hourly_circuit_open", _cap_never_open)

    backend = worker_module._JobRunners(
        runtime_or_contract=_runtime_runner, ci_failure=_runtime_runner
    )
    claimed = await worker_module._process_next_job(mcp=None, backend=backend)  # type: ignore[arg-type]

    assert claimed is True
    assert len(created_clients) == 1
    assert created_clients[0].repo == "someone/their-repo"

    async with session_scope() as session:
        refreshed = await session.get(HealJob, job_id)
        assert refreshed is not None


@pytest.mark.parametrize(
    ("backend", "module_name", "runtime_fn", "ci_fn"),
    [
        ("claude_cli", "healer.agent_free", "run_heal_job_free", "run_ci_heal_job_free"),
        ("codex_cli", "healer.agent_codex", "run_heal_job_codex", "run_ci_heal_job_codex"),
        ("gemini_cli", "healer.agent_gemini", "run_heal_job_gemini", "run_ci_heal_job_gemini"),
    ],
)
def test_select_backend_dispatches_free_mode_backends_by_ai_backend_setting(
    monkeypatch: pytest.MonkeyPatch,
    backend: str,
    module_name: str,
    runtime_fn: str,
    ci_fn: str,
) -> None:
    """`_select_backend` must resolve to the right module's functions for
    each of the three free-mode `AI_BACKEND` values, importing lazily (so
    selecting one backend never requires another backend's CLI/module)."""
    monkeypatch.setattr(core_settings, "ai_backend", backend)

    runners = worker_module._select_backend()

    module = __import__(module_name, fromlist=[runtime_fn, ci_fn])
    assert runners.runtime_or_contract is getattr(module, runtime_fn)
    assert runners.ci_failure is getattr(module, ci_fn)


def test_select_backend_api_mode_builds_anthropic_client_and_binds_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(core_settings, "ai_backend", "api")
    monkeypatch.setattr(core_settings, "anthropic_api_key", "sk-test-fake-key-not-real")

    runners = worker_module._select_backend()

    assert runners.runtime_or_contract.func.__name__ == "run_heal_job"  # type: ignore[attr-defined]
    assert runners.ci_failure.func.__name__ == "run_ci_heal_job"  # type: ignore[attr-defined]


def test_select_backend_rejects_unknown_ai_backend_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(core_settings, "ai_backend", "not-a-real-backend")

    with pytest.raises(ValueError, match="unknown AI_BACKEND"):
        worker_module._select_backend()


def test_select_backend_by_explicit_name_builds_api_key_backends(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`_select_backend(name)` (the chain-dispatch path) reuses runtime_agent/
    ci_agent unchanged for gemini_api/groq_api -- only the anthropic_client
    adapter differs."""
    monkeypatch.setattr(core_settings, "groq_api_key", "gsk_test")
    monkeypatch.setattr(core_settings, "gemini_api_key", "test-key")

    from healer.ci_agent import run_ci_heal_job
    from healer.runtime_agent import run_heal_job

    groq_runners = worker_module._select_backend("groq_api")
    assert groq_runners.runtime_or_contract.func is run_heal_job  # type: ignore[attr-defined]
    assert groq_runners.ci_failure.func is run_ci_heal_job  # type: ignore[attr-defined]

    gemini_runners = worker_module._select_backend("gemini_api")
    assert gemini_runners.runtime_or_contract.func is run_heal_job  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_chain_dispatch_picks_first_eligible_backend_per_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With `backend=None`, `_process_next_job` resolves the backend from
    `settings.ai_chain_list` and records a `backend_attempt` audit row."""
    from core.models import AuditLog
    from healer import backend_chain

    backend_chain.clear_cooldowns()
    monkeypatch.setattr(core_settings, "ai_chain", "claude_cli,groq_api")

    async def _cap_never_open(session: object, *, max_per_hour: int) -> bool:
        return False

    monkeypatch.setattr(worker_module, "global_hourly_circuit_open", _cap_never_open)

    called_with: dict[str, Any] = {}

    async def _fake_run_heal_job_free(job_id: int, *, mcp: Any, github: Any) -> None:
        called_with["job_id"] = job_id

    monkeypatch.setattr("healer.agent_free.run_heal_job_free", _fake_run_heal_job_free)

    fingerprint = f"test-chain-{uuid.uuid4().hex}"
    async with session_scope() as session:
        job = await enqueue_heal_job(
            session, type=HealJobType.RUNTIME_ERROR, fingerprint=fingerprint
        )
        job_id = job.id

    claimed = await worker_module._process_next_job(mcp=None)  # type: ignore[arg-type]
    assert claimed is True
    assert called_with["job_id"] == job_id

    async with session_scope() as session:
        stmt = AuditLog.__table__.select().where(
            AuditLog.heal_job_id == job_id, AuditLog.action == "backend_attempt"
        )
        rows = (await session.execute(stmt)).mappings().all()
        assert any(r["details"]["backend"] == "claude_cli" for r in rows)
        refreshed = await session.get(HealJob, job_id)
        assert refreshed is not None
        refreshed.status = HealJobStatus.FAILED  # cleanup: never leave it QUEUED/RUNNING


@pytest.mark.asyncio
async def test_backend_cooldown_error_requeues_job_for_the_next_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from healer import backend_chain

    backend_chain.clear_cooldowns()
    monkeypatch.setattr(core_settings, "ai_chain", "groq_api,claude_cli")
    monkeypatch.setattr(core_settings, "groq_api_key", "gsk_test")

    async def _cap_never_open(session: object, *, max_per_hour: int) -> bool:
        return False

    monkeypatch.setattr(worker_module, "global_hourly_circuit_open", _cap_never_open)

    async def _raise_cooldown(job_id: int, *, anthropic_client: Any, mcp: Any, github: Any) -> None:
        raise backend_chain.BackendCooldownError("quota exceeded", retry_after_seconds=999)

    monkeypatch.setattr("healer.runtime_agent.run_heal_job", _raise_cooldown)

    fingerprint = f"test-cooldown-{uuid.uuid4().hex}"
    async with session_scope() as session:
        job = await enqueue_heal_job(
            session, type=HealJobType.RUNTIME_ERROR, fingerprint=fingerprint
        )
        job_id = job.id

    claimed = await worker_module._process_next_job(mcp=None)  # type: ignore[arg-type]
    assert claimed is False  # back off, don't busy-spin -- same as the global-cap path
    assert backend_chain.is_cooling_down("groq_api") is True

    async with session_scope() as session:
        refreshed = await session.get(HealJob, job_id)
        assert refreshed is not None
        assert refreshed.status == HealJobStatus.QUEUED
        assert refreshed.started_at is None

    # Next dequeue: groq_api is cooling down, so it must pick claude_cli.
    called: dict[str, Any] = {}

    async def _fake_free(job_id: int, *, mcp: Any, github: Any) -> None:
        called["ran"] = True

    monkeypatch.setattr("healer.agent_free.run_heal_job_free", _fake_free)

    claimed_again = await worker_module._process_next_job(mcp=None)  # type: ignore[arg-type]
    assert claimed_again is True
    assert called.get("ran") is True

    async with session_scope() as session:
        refreshed = await session.get(HealJob, job_id)
        assert refreshed is not None
        refreshed.status = HealJobStatus.FAILED  # cleanup
    backend_chain.clear_cooldowns()


@pytest.mark.asyncio
async def test_all_backends_exhausted_pauses_the_job(monkeypatch: pytest.MonkeyPatch) -> None:
    from healer import backend_chain

    backend_chain.clear_cooldowns()
    monkeypatch.setattr(core_settings, "ai_chain", "groq_api")
    monkeypatch.setattr(core_settings, "groq_api_key", None)  # no key -> never eligible

    async def _cap_never_open(session: object, *, max_per_hour: int) -> bool:
        return False

    monkeypatch.setattr(worker_module, "global_hourly_circuit_open", _cap_never_open)

    fingerprint = f"test-exhausted-{uuid.uuid4().hex}"
    async with session_scope() as session:
        job = await enqueue_heal_job(
            session, type=HealJobType.RUNTIME_ERROR, fingerprint=fingerprint
        )
        job_id = job.id

    claimed = await worker_module._process_next_job(mcp=None)  # type: ignore[arg-type]
    assert claimed is True  # the job WAS processed -- paused, not left dangling

    async with session_scope() as session:
        refreshed = await session.get(HealJob, job_id)
        assert refreshed is not None
        assert refreshed.status == HealJobStatus.PAUSED_BUDGET

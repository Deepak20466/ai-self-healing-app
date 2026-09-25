"""healer/app.py's connect-a-repo endpoints: auth gating, request/response
shape, and that a "Fix" click enqueues a real heal_job. The actual clone/scan
subprocess work is unit-tested directly in test_repo_connect.py/test_scanner.py
-- here `connect_repo`/`_run_scan_and_notify` are monkeypatched so these stay
fast, offline, and free of real git/network calls.
"""

from __future__ import annotations

import uuid
from typing import Any

import httpx
import pytest
from argon2 import PasswordHasher
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import settings as core_settings
from core.db import get_db
from core.models import Finding, FindingCategory, FindingSeverity, HealJob, MonitoredApp

PASSWORD = "correct horse battery staple"


@pytest.fixture(autouse=True)
def _configure_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(core_settings, "admin_password_hash", PasswordHasher().hash(PASSWORD))
    monkeypatch.setattr(core_settings, "session_secret", "test-session-secret")
    monkeypatch.setattr(core_settings, "environment", "development")
    monkeypatch.setattr(core_settings, "github_token", "gh-fake-token")


@pytest.fixture
def app_client(db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch):
    from healer import app as healer_app_module

    async def _noop_scan(app_id: int) -> None:
        return None

    monkeypatch.setattr(healer_app_module, "_run_scan_and_notify", _noop_scan)

    async def _override_db():
        yield db_session

    healer_app_module.app.dependency_overrides[get_db] = _override_db
    transport = httpx.ASGITransport(app=healer_app_module.app)
    client = httpx.AsyncClient(transport=transport, base_url="http://healer.test")
    try:
        yield client
    finally:
        healer_app_module.app.dependency_overrides.clear()


async def _login(client: httpx.AsyncClient) -> None:
    resp = await client.post("/api/auth/login", json={"username": "admin", "password": PASSWORD})
    assert resp.status_code == 200


async def test_connect_app_requires_auth(app_client: httpx.AsyncClient) -> None:
    async with app_client as client:
        resp = await client.post("/api/apps", json={"repo_url": "https://github.com/a/b"})
    assert resp.status_code == 401


async def test_connect_app_success(
    app_client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch, db_session: AsyncSession
) -> None:
    from healer import app as healer_app_module

    async def _fake_connect_repo(
        session: AsyncSession, *, repo_url: str, name: str | None, github_token: str
    ) -> MonitoredApp:
        app_row = MonitoredApp(
            name=name or "some-app",
            language="python",
            local_repo_path="connected_apps/some-app",
            github_repo="acme/some-app",
            allowed_write_paths=["connected_apps/some-app/"],
            test_command="pytest",
            ingest_token=uuid.uuid4().hex,
            repo_url=repo_url,
        )
        session.add(app_row)
        await session.flush()
        return app_row

    monkeypatch.setattr(healer_app_module, "connect_repo", _fake_connect_repo)

    async with app_client as client:
        await _login(client)
        resp = await client.post(
            "/api/apps", json={"repo_url": "https://github.com/acme/some-app", "name": "some-app"}
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "some-app"
    assert body["status"] == "scanning"
    assert body["connected"] is True


async def test_connect_app_returns_400_on_repo_connect_error(
    app_client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from core.repo_connect import RepoConnectError
    from healer import app as healer_app_module

    async def _fake_connect_repo(*args: Any, **kwargs: Any) -> MonitoredApp:
        raise RepoConnectError("Can't access acme/private with the configured GITHUB_TOKEN")

    monkeypatch.setattr(healer_app_module, "connect_repo", _fake_connect_repo)

    async with app_client as client:
        await _login(client)
        resp = await client.post("/api/apps", json={"repo_url": "https://github.com/acme/private"})
    assert resp.status_code == 400
    assert "GITHUB_TOKEN" in resp.json()["detail"]


async def _make_app(db_session: AsyncSession, **overrides: Any) -> MonitoredApp:
    defaults: dict[str, Any] = dict(
        name=f"app-{uuid.uuid4().hex[:8]}",
        language="python",
        local_repo_path="connected_apps/testapp",
        github_repo="acme/testapp",
        allowed_write_paths=["connected_apps/testapp/"],
        test_command="pytest",
        ingest_token=uuid.uuid4().hex,
        repo_url="https://github.com/acme/testapp",
    )
    defaults.update(overrides)
    app_row = MonitoredApp(**defaults)
    db_session.add(app_row)
    await db_session.flush()
    return app_row


async def test_list_and_get_app_detail(
    app_client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    app_row = await _make_app(db_session)
    finding = Finding(
        app_id=app_row.id,
        fingerprint=uuid.uuid4().hex,
        category=FindingCategory.LINT,
        severity=FindingSeverity.LOW,
        tool="ruff",
        message="unused import",
    )
    db_session.add(finding)
    await db_session.flush()

    async with app_client as client:
        await _login(client)
        list_resp = await client.get("/api/apps")
        assert list_resp.status_code == 200
        assert any(a["id"] == app_row.id for a in list_resp.json())

        detail_resp = await client.get(f"/api/apps/{app_row.id}")
        assert detail_resp.status_code == 200
        body = detail_resp.json()
        assert body["id"] == app_row.id
        assert len(body["findings"]) == 1
        assert body["findings"][0]["tool"] == "ruff"


async def test_update_app_toggles_auto_fix(
    app_client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    app_row = await _make_app(db_session)
    async with app_client as client:
        await _login(client)
        resp = await client.patch(f"/api/apps/{app_row.id}", json={"auto_fix_high_severity": True})
    assert resp.status_code == 200
    assert resp.json()["auto_fix_high_severity"] is True


async def test_fix_finding_enqueues_a_heal_job(
    app_client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    app_row = await _make_app(db_session)
    finding = Finding(
        app_id=app_row.id,
        fingerprint=uuid.uuid4().hex,
        category=FindingCategory.DEPENDENCY,
        severity=FindingSeverity.HIGH,
        tool="pip-audit",
        message="requests: known vulnerability",
    )
    db_session.add(finding)
    await db_session.flush()

    async with app_client as client:
        await _login(client)
        resp = await client.post(f"/api/findings/{finding.id}/fix")
    assert resp.status_code == 200
    heal_job_id = resp.json()["heal_job_id"]

    job = await db_session.get(HealJob, heal_job_id)
    assert job is not None
    assert job.fingerprint == finding.fingerprint
    assert job.app_id == app_row.id

    await db_session.refresh(finding)
    assert finding.status.value == "fix_requested"
    assert finding.heal_job_id == heal_job_id


async def test_onboard_app_opens_a_pr(
    app_client: httpx.AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    from healer import app as healer_app_module

    app_row = await _make_app(db_session)

    async def _fake_open_pr(app: MonitoredApp) -> dict[str, Any]:
        assert app.id == app_row.id
        return {"number": 42, "html_url": "https://github.com/acme/testapp/pull/42"}

    monkeypatch.setattr(healer_app_module, "open_onboarding_pull_request", _fake_open_pr)

    async with app_client as client:
        await _login(client)
        resp = await client.post(f"/api/apps/{app_row.id}/onboard-pr")
    assert resp.status_code == 200
    assert resp.json()["pr_number"] == 42

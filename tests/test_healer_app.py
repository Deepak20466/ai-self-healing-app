"""healer/app.py: auth flow, 401 on unauthenticated access, dashboard/metrics
REST endpoints served from (faked) MCP tool data. No real socket.io, no real
mcp-pod connection, no real worker loop — `app` (the plain FastAPI app, not
the lifespan-wrapped `asgi_app`) is exercised directly over ASGI with
`get_db`/`get_mcp_client` dependency-overridden, the same pattern
`tests/conftest.py` already uses for sentinel-pod/target_app.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from argon2 import PasswordHasher
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import settings as core_settings
from core.db import get_db

PASSWORD = "correct horse battery staple"


class FakeMCPClient:
    def __init__(self, responses: dict[str, Any]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        self.calls.append((name, arguments))
        return self.responses.get(name, {})


@pytest.fixture(autouse=True)
def _configure_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(core_settings, "admin_password_hash", PasswordHasher().hash(PASSWORD))
    monkeypatch.setattr(core_settings, "session_secret", "test-session-secret")
    monkeypatch.setattr(core_settings, "environment", "development")


@pytest.fixture
def fake_mcp() -> FakeMCPClient:
    return FakeMCPClient(
        {
            "get_metrics": {"mttr_minutes": 12.5, "fix_success_rate": 0.9},
            "list_open_errors": [{"id": 1, "exception_type": "ZeroDivisionError"}],
            "get_health": {"healthy": True, "pods": []},
            "get_deployment_status": {"deployment": None},
            "list_workflow_runs": [],
        }
    )


@pytest.fixture
def app_client(db_session: AsyncSession, fake_mcp: FakeMCPClient):
    from healer import app as healer_app_module

    async def _override_db():
        yield db_session

    healer_app_module.app.dependency_overrides[get_db] = _override_db
    healer_app_module.app.dependency_overrides[healer_app_module.get_mcp_client] = lambda: fake_mcp

    transport = httpx.ASGITransport(app=healer_app_module.app)
    client = httpx.AsyncClient(transport=transport, base_url="http://healer.test")
    try:
        yield client
    finally:
        healer_app_module.app.dependency_overrides.clear()


async def test_healthz_is_public(app_client: httpx.AsyncClient) -> None:
    async with app_client as client:
        resp = await client.get("/healthz")
    assert resp.status_code == 200


async def test_unauthenticated_metrics_returns_401(app_client: httpx.AsyncClient) -> None:
    async with app_client as client:
        resp = await client.get("/api/metrics")
    assert resp.status_code == 401


async def test_unauthenticated_errors_returns_401(app_client: httpx.AsyncClient) -> None:
    async with app_client as client:
        resp = await client.get("/api/errors")
    assert resp.status_code == 401


async def test_login_with_wrong_password_returns_401(app_client: httpx.AsyncClient) -> None:
    async with app_client as client:
        resp = await client.post("/api/auth/login", json={"username": "admin", "password": "nope"})
    assert resp.status_code == 401


async def test_login_then_access_dashboard_endpoints(
    app_client: httpx.AsyncClient, fake_mcp: FakeMCPClient
) -> None:
    async with app_client as client:
        login_resp = await client.post(
            "/api/auth/login", json={"username": "admin", "password": PASSWORD}
        )
        assert login_resp.status_code == 200
        assert "selfheal_session" in login_resp.cookies

        metrics_resp = await client.get("/api/metrics")
        assert metrics_resp.status_code == 200
        assert metrics_resp.json()["mttr_minutes"] == 12.5

        errors_resp = await client.get("/api/errors")
        assert errors_resp.status_code == 200
        assert errors_resp.json()[0]["exception_type"] == "ZeroDivisionError"

        health_resp = await client.get("/api/health")
        assert health_resp.status_code == 200

        session_resp = await client.get("/api/auth/session")
        assert session_resp.status_code == 200
        assert session_resp.json()["username"] == "admin"

        logout_resp = await client.post("/api/auth/logout")
        assert logout_resp.status_code == 200

        after_logout = await client.get("/api/metrics")
        assert after_logout.status_code == 401


async def test_new_chat_session_and_history(app_client: httpx.AsyncClient) -> None:
    async with app_client as client:
        await client.post("/api/auth/login", json={"username": "admin", "password": PASSWORD})
        create_resp = await client.post("/api/chat/session")
        assert create_resp.status_code == 200
        session_id = create_resp.json()["session_id"]

        history_resp = await client.get("/api/chat/history", params={"session_id": session_id})
        assert history_resp.status_code == 200
        assert history_resp.json() == []


async def test_index_declares_a_favicon_so_browsers_dont_request_a_404(
    app_client: httpx.AsyncClient,
) -> None:
    async with app_client as client:
        resp = await client.get("/")
    assert resp.status_code == 200
    assert 'rel="icon"' in resp.text

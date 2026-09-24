"""Shared test fixtures.

`db_session` wraps each test in an outer transaction on a dedicated
connection and rolls it back afterward, using SQLAlchemy 2.0's
`join_transaction_mode="create_savepoint"` so that code under test (sentinel
storage functions, mostly) can call `session.commit()` freely without
actually persisting anything past the test.

`sentinel_client_app` / `target_app_client` wire target_app's
`SentinelMiddleware` to sentinel-pod's FastAPI app entirely in-process via
`httpx.ASGITransport`, and override both apps' `get_db` dependency to the
same `db_session` — so an end-to-end test (hit a target_app route, assert a
row via sentinel) runs inside one rolled-back transaction with no real
server processes or sockets involved.
"""

from __future__ import annotations

import asyncio
import subprocess
import uuid
from collections.abc import AsyncGenerator, Generator
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core.db import engine, get_db


def pytest_sessionstart(session: object) -> None:
    """Seed the deterministic demo dataset once, before any test runs.

    Runs in its own throwaway event loop (independent of pytest-asyncio's
    per-test loop) and disposes the engine afterward, so pooled connections
    are never reused across event loops.
    """
    from scripts.seed_demo import seed

    asyncio.run(seed())


@pytest_asyncio.fixture
async def db_session() -> AsyncGenerator[AsyncSession, None]:
    async with engine.connect() as conn:
        await conn.begin()
        session_factory = async_sessionmaker(
            bind=conn,
            join_transaction_mode="create_savepoint",
            expire_on_commit=False,
        )
        session = session_factory()
        try:
            yield session
        finally:
            await session.close()
            await conn.rollback()


@pytest_asyncio.fixture
async def sentinel_asgi_app(db_session: AsyncSession):
    from sentinel.app import app as sentinel_app

    async def _override() -> AsyncGenerator[AsyncSession, None]:
        yield db_session

    sentinel_app.dependency_overrides[get_db] = _override
    yield sentinel_app
    sentinel_app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def sentinel_http_client(
    sentinel_asgi_app,
) -> AsyncGenerator[httpx.AsyncClient, None]:
    transport = httpx.ASGITransport(app=sentinel_asgi_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://sentinel.test") as client:
        yield client


@pytest_asyncio.fixture
async def sentinel_client_for_target_app(sentinel_http_client: httpx.AsyncClient):
    from sentinel.client import SentinelClient

    yield SentinelClient(client=sentinel_http_client)


@pytest_asyncio.fixture
async def target_app_client(
    db_session: AsyncSession, sentinel_client_for_target_app
) -> AsyncGenerator[httpx.AsyncClient, None]:
    from apps.target_app.main import create_app

    app = create_app(sentinel_client=sentinel_client_for_target_app)

    async def _override() -> AsyncGenerator[AsyncSession, None]:
        yield db_session

    app.dependency_overrides[get_db] = _override

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://target.test") as client:
        yield client


@pytest.fixture
def git_worktree() -> Generator[tuple[str, Path], None, None]:
    """Create a real `git worktree` under `worktrees/` for sandbox/git_utils tests."""
    from mcp_server.sandbox import REPO_ROOT, WORKTREES_ROOT

    WORKTREES_ROOT.mkdir(exist_ok=True)
    name = f"test-{uuid.uuid4().hex[:8]}"
    worktree_path = WORKTREES_ROOT / name
    branch_name = f"test-worktree/{name}"

    subprocess.run(
        ["git", "worktree", "add", "-b", branch_name, str(worktree_path), "master"],
        cwd=str(REPO_ROOT),
        check=True,
        capture_output=True,
    )
    try:
        yield name, worktree_path
    finally:
        subprocess.run(
            ["git", "worktree", "remove", "--force", str(worktree_path)],
            cwd=str(REPO_ROOT),
            capture_output=True,
            check=False,
        )
        subprocess.run(
            ["git", "branch", "-D", branch_name],
            cwd=str(REPO_ROOT),
            capture_output=True,
            check=False,
        )

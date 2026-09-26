"""Shared test fixtures.

**Read this before adding a fixture that touches the database or the
network.** The first thing this module does — before importing anything
from `core` or any pod package — is force `DATABASE_URL` to a throwaway
`..._test` database and force `ANTHROPIC_API_KEY`/`GITHUB_TOKEN`/
`GITHUB_REPO` to obviously-fake values, for the whole pytest process. This
has to happen before the *first* import of `core.config`/`core.db`
anywhere: both are module-level singletons bound at import time (`core.db.
engine` is literally constructed from `settings.database_url` as soon as
`core.db` is imported), so overriding the environment any later would be too
late for whichever module happened to import first. The dev database is
never opened by the test suite, and a test that forgets to mock Anthropic or
GitHub fails closed (401 against the real API) instead of silently spending
money or opening a real PR — see CLAUDE.md "Test database" and "Anthropic
and GitHub are always mocked in tests".

`pytest_sessionstart` creates the test database if it doesn't exist yet
(requires the app DB role to have `CREATEDB`, granted once by
`scripts/bootstrap.ps1`/`.sh` — never Postgres superuser credentials),
migrates it with Alembic, then seeds the deterministic demo dataset — all
before any test runs.

`db_session` wraps each test in an outer transaction on a dedicated
connection and rolls it back afterward, using SQLAlchemy 2.0's
`join_transaction_mode="create_savepoint"` so that code under test (sentinel
storage functions, mostly) can call `session.commit()` freely without
actually persisting anything past the test. Tests that need a real,
cross-connection commit (e.g. exercising `SELECT ... FOR UPDATE SKIP
LOCKED`) use `core.db.session_scope()` directly instead and rely on random
fingerprint/id suffixes for isolation — see CLAUDE.md's ambiguities log for
why that pattern was chosen over per-test truncation.

`sentinel_client_app` / `target_app_client` wire target_app's
`SentinelMiddleware` to sentinel-pod's FastAPI app entirely in-process via
`httpx.ASGITransport`, and override both apps' `get_db` dependency to the
same `db_session` — so an end-to-end test (hit a target_app route, assert a
row via sentinel) runs inside one rolled-back transaction with no real
server processes or sockets involved.
"""

from __future__ import annotations

import os
from pathlib import Path

from sqlalchemy.engine import make_url

_REPO_ROOT = Path(__file__).resolve().parent.parent


def _dotenv_value(key: str) -> str | None:
    """Read a single key from `.env` without importing pydantic-settings.

    Only used to compute the test-database override below, which must run
    before `core.config` (and therefore pydantic-settings' own `.env`
    loading) is ever imported.
    """
    env_path = _REPO_ROOT / ".env"
    if not env_path.exists():
        return None
    for line in env_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith(f"{key}=") and not stripped.startswith("#"):
            return stripped.split("=", 1)[1].strip().strip("'\"")
    return None


def _test_database_url() -> str:
    """Resolve the throwaway test database URL, never the dev `DATABASE_URL`.

    Prefers an explicit `TEST_DATABASE_URL` (from `.env` or the real
    environment); otherwise derives `<dbname>_test` from `DATABASE_URL`.
    """
    explicit = os.environ.get("TEST_DATABASE_URL") or _dotenv_value("TEST_DATABASE_URL")
    if explicit:
        return explicit
    raw = (
        os.environ.get("DATABASE_URL")
        or _dotenv_value("DATABASE_URL")
        or "postgresql+asyncpg://selfheal:selfheal@localhost:5432/selfheal"
    )
    url = make_url(raw)
    db_name = url.database or "selfheal"
    if not db_name.endswith("_test"):
        db_name = f"{db_name}_test"
    return str(url.set(database=db_name))


# Force every pod/module the test suite touches onto a throwaway database and
# fake AI/GitHub credentials. Unconditional (not `setdefault`) so a real key
# exported in the ambient shell (e.g. to run the app in another terminal)
# can never leak into a test run.
os.environ["DATABASE_URL"] = _test_database_url()
os.environ["ANTHROPIC_API_KEY"] = "sk-ant-test-mock-do-not-use"
os.environ["GITHUB_TOKEN"] = "github_pat_test-mock-do-not-use"
os.environ["GITHUB_REPO"] = "test-org/test-repo"

# --- safe to import project modules from here on ---------------------------

import asyncio  # noqa: E402
import subprocess  # noqa: E402
import tempfile  # noqa: E402
import uuid  # noqa: E402
from collections.abc import AsyncGenerator, Generator  # noqa: E402

import httpx  # noqa: E402
import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker  # noqa: E402

from core.db import engine, get_db  # noqa: E402


def _ensure_test_database_exists() -> None:
    """`CREATE DATABASE` the test DB if it doesn't exist yet. Idempotent."""
    import asyncpg

    from core.config import settings

    url = make_url(settings.database_url)

    async def _create() -> None:
        conn = await asyncpg.connect(
            user=url.username,
            password=url.password,
            host=url.host,
            port=url.port,
            database="postgres",
        )
        try:
            exists = await conn.fetchval(
                "SELECT 1 FROM pg_database WHERE datname = $1", url.database
            )
            if not exists:
                # asyncpg can't bind identifiers as query parameters; this
                # name always comes from our own settings, never user input.
                await conn.execute(f'CREATE DATABASE "{url.database}"')
        except asyncpg.exceptions.DuplicateDatabaseError:
            pass  # created concurrently by another process - fine
        finally:
            await conn.close()

    try:
        asyncio.run(_create())
    except asyncpg.exceptions.InsufficientPrivilegeError as exc:
        raise RuntimeError(
            f"The '{url.username}' Postgres role can't CREATE DATABASE, so "
            f"pytest can't provision '{url.database}'. Re-run "
            "scripts/bootstrap.ps1 (or bootstrap.sh) with your Postgres "
            "superuser password to grant it once - see CLAUDE.md 'Test "
            "database'. This never needs to reach the assistant."
        ) from exc


def _migrate_test_database() -> None:
    """Run Alembic migrations against the (now-overridden) test database."""
    from alembic.config import Config

    from alembic import command

    config = Config(str(_REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(_REPO_ROOT / "alembic"))
    command.upgrade(config, "head")


def pytest_sessionstart(session: object) -> None:
    """Provision the test database, then seed the deterministic dataset.

    Runs in its own throwaway event loop (independent of pytest-asyncio's
    per-test loop) and disposes the engine afterward, so pooled connections
    are never reused across event loops.
    """
    _ensure_test_database_exists()
    _migrate_test_database()

    from scripts.seed_demo import seed

    async def _seed_all() -> None:
        from core.db import dispose_engine, session_scope
        from core.monitored_apps import sync_monitored_apps

        try:
            await seed()
            async with session_scope() as db_session:
                await sync_monitored_apps(db_session)
        finally:
            # Pooled asyncpg connections are bound to this throwaway loop;
            # drop them so the session-scoped test loop never inherits one.
            await dispose_engine()

    asyncio.run(_seed_all())


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


@pytest_asyncio.fixture
async def isolated_budget_date(monkeypatch: pytest.MonkeyPatch) -> None:
    """Freeze budget's date to a synthetic date based on UUID so each test
    run uses a different day and never collides with other test dates or
    real production data in the shared selfheal_test DB."""
    from datetime import datetime

    import healer.budget as budget_module

    # Deterministic but unique per test run: every pytest run gets a
    # different synthetic date (uuid.uuid5 seeded from a fresh uuid.uuid4()).
    hash_val = uuid.uuid5(uuid.NAMESPACE_DNS, uuid.uuid4().hex).int
    year = 2200 + (hash_val % 50)
    month = 1 + (hash_val % 12)
    day = 1 + ((hash_val // 12) % 28)  # Keep it within valid day range

    class _FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):  # type: ignore
            return datetime(year, month, day, tzinfo=tz)

    monkeypatch.setattr(budget_module, "datetime", _FrozenDatetime)
    yield


@pytest_asyncio.fixture
async def isolated_cli_call_date(monkeypatch: pytest.MonkeyPatch) -> None:
    """Same isolation as `isolated_budget_date`, for `healer.agent_free`'s
    MAX_CLI_CALLS_PER_DAY counter (a count of `cli_invocation` audit_log rows
    created since UTC midnight — see agent_free.py's module docstring on why
    that can't reuse `healer.budget`'s dollar-denominated `daily_spend`
    table). Hit this for real: repeatedly re-running
    tests/test_healer_agent_free.py against the shared selfheal_test DB
    during development accumulated exactly `MAX_CLI_CALLS_PER_DAY` (50) real,
    never-rolled-back `cli_invocation` rows for the real current date, which
    then made every subsequent free-mode e2e test in the same file
    incorrectly see the daily cap as already exhausted from its very first
    attempt. Every test that exercises `run_heal_job_free`/
    `run_ci_heal_job_free` needs this fixture for the same reason
    `isolated_budget_date` is needed by any test calling `run_heal_job`/
    `run_ci_heal_job`."""
    from datetime import datetime

    import healer.agent_free as agent_free_module

    hash_val = uuid.uuid5(uuid.NAMESPACE_DNS, uuid.uuid4().hex).int
    year = 2200 + (hash_val % 50)
    month = 1 + (hash_val % 12)
    day = 1 + ((hash_val // 12) % 28)

    class _FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):  # type: ignore
            return datetime(year, month, day, tzinfo=tz)

    monkeypatch.setattr(agent_free_module, "datetime", _FrozenDatetime)
    yield


@pytest.fixture
def git_worktree() -> Generator[tuple[str, Path], None, None]:
    """Create a real `git worktree` under `worktrees/` for sandbox/git_utils tests."""
    from mcp_server.sandbox import REPO_ROOT, WORKTREES_ROOT

    WORKTREES_ROOT.mkdir(exist_ok=True)
    name = f"test-{uuid.uuid4().hex[:8]}"
    worktree_path = WORKTREES_ROOT / name
    branch_name = f"test-worktree/{name}"

    subprocess.run(
        ["git", "worktree", "add", "-b", branch_name, str(worktree_path), "main"],
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


@pytest.fixture
def fake_git_remote() -> Generator[str, None, None]:
    """Add a local bare repo as a throwaway git remote, for testing `git push`
    without ever touching the real `origin` (the public GitHub repo).

    Yields the remote's name; `healer.worktree.commit_and_push`'s tests (and
    the runtime_agent integration tests) push to this name instead of
    "origin".
    """
    from mcp_server.sandbox import REPO_ROOT

    with tempfile.TemporaryDirectory(prefix="healer-fake-remote-") as tmp_dir:
        bare_repo = Path(tmp_dir) / "repo.git"
        subprocess.run(["git", "init", "--bare", str(bare_repo)], check=True, capture_output=True)
        remote_name = f"healer-test-remote-{uuid.uuid4().hex[:8]}"
        subprocess.run(
            ["git", "remote", "add", remote_name, str(bare_repo)],
            cwd=str(REPO_ROOT),
            check=True,
            capture_output=True,
        )
        try:
            yield remote_name
        finally:
            subprocess.run(
                ["git", "remote", "remove", remote_name],
                cwd=str(REPO_ROOT),
                capture_output=True,
                check=False,
            )

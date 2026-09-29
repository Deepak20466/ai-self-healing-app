"""Tests for core/repo_connect.py: URL parsing, name slugging, stack
detection, and the access-check error message -- all pure/local, no real
GitHub network call (respx mocks the one HTTP call `check_repo_access` makes).
"""

from __future__ import annotations

import httpx
import pytest

from core.config import settings as config_settings
from core.repo_connect import (
    RepoConnectError,
    SubprojectsFound,
    app_fingerprint,
    check_repo_access,
    detect_stack,
    find_subprojects,
    parse_github_url,
    select_subproject,
    slugify_app_name,
)


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://github.com/octocat/Hello-World", "octocat/Hello-World"),
        ("https://github.com/octocat/Hello-World.git", "octocat/Hello-World"),
        ("https://github.com/octocat/Hello-World/", "octocat/Hello-World"),
        ("git@github.com:octocat/Hello-World.git", "octocat/Hello-World"),
    ],
)
def test_parse_github_url_accepts_common_forms(url: str, expected: str) -> None:
    assert parse_github_url(url) == expected


def test_parse_github_url_rejects_a_non_github_url() -> None:
    with pytest.raises(RepoConnectError):
        parse_github_url("https://gitlab.com/octocat/Hello-World")


@pytest.mark.parametrize(
    "owner_repo,expected",
    [
        ("octocat/Hello-World", "hello-world"),
        ("acme/My_Cool.App", "my_cool-app"),
    ],
)
def test_slugify_app_name(owner_repo: str, expected: str) -> None:
    assert slugify_app_name(owner_repo) == expected


def test_detect_stack_python_from_requirements(tmp_path) -> None:
    (tmp_path / "requirements.txt").write_text("flask==3.0.0\n")
    stack = detect_stack(tmp_path)
    assert stack.language == "python"
    assert stack.test_command == "pytest"
    assert stack.lint_command == "ruff check ."
    assert stack.install_command == "pip install -r requirements.txt"


def test_detect_stack_python_prefers_pyproject(tmp_path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'x'\n")
    (tmp_path / "requirements.txt").write_text("flask\n")
    stack = detect_stack(tmp_path)
    assert stack.install_command == "pip install -e ."


def test_detect_stack_node_reads_package_json_scripts(tmp_path) -> None:
    (tmp_path / "package.json").write_text('{"scripts": {"test": "jest", "lint": "eslint ."}}')
    stack = detect_stack(tmp_path)
    assert stack.language == "javascript"
    assert stack.test_command == "npm test"
    assert stack.lint_command == "npm run lint"


def test_detect_stack_node_without_test_script(tmp_path) -> None:
    (tmp_path / "package.json").write_text("{}")
    stack = detect_stack(tmp_path)
    assert stack.test_command == "npm test --if-present"
    assert stack.lint_command is None


def test_detect_stack_go(tmp_path) -> None:
    (tmp_path / "go.mod").write_text("module example.com/x\n")
    stack = detect_stack(tmp_path)
    assert stack.language == "go"
    assert stack.test_command == "go test ./..."


def test_detect_stack_unknown_when_nothing_recognized(tmp_path) -> None:
    stack = detect_stack(tmp_path)
    assert stack.language == "unknown"
    assert stack.install_command is None


def test_find_subprojects_finds_a_root_manifest(tmp_path) -> None:
    (tmp_path / "package.json").write_text("{}")
    subprojects = find_subprojects(tmp_path)
    assert [sp.rel_path for sp in subprojects] == [""]
    assert subprojects[0].stack.language == "javascript"


def test_find_subprojects_finds_nested_manifests_up_to_two_levels(tmp_path) -> None:
    (tmp_path / "backend").mkdir()
    (tmp_path / "backend" / "requirements.txt").write_text("fastapi\n")
    (tmp_path / "frontend").mkdir()
    (tmp_path / "frontend" / "package.json").write_text("{}")
    (tmp_path / "mobile" / "app").mkdir(parents=True)
    (tmp_path / "mobile" / "app" / "go.mod").write_text("module x\n")

    subprojects = {sp.rel_path: sp.stack.language for sp in find_subprojects(tmp_path)}
    assert subprojects == {
        "backend": "python",
        "frontend": "javascript",
        "mobile/app": "go",
    }


def test_find_subprojects_skips_noise_directories(tmp_path) -> None:
    (tmp_path / "node_modules" / "some-pkg").mkdir(parents=True)
    (tmp_path / "node_modules" / "some-pkg" / "package.json").write_text("{}")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "package.json").write_text("{}")

    assert find_subprojects(tmp_path) == []


def test_find_subprojects_does_not_recurse_past_max_depth(tmp_path) -> None:
    deep = tmp_path / "a" / "b" / "c"
    deep.mkdir(parents=True)
    (deep / "package.json").write_text("{}")
    # "a/b/c" is 3 levels below root -- past the default 2-level limit.
    assert find_subprojects(tmp_path, max_depth=2) == []
    assert [sp.rel_path for sp in find_subprojects(tmp_path, max_depth=3)] == ["a/b/c"]


async def test_connect_repo_raises_subprojects_found_for_a_root_less_monorepo(
    tmp_path, monkeypatch: pytest.MonkeyPatch, respx_mock
) -> None:
    import core.repo_connect as repo_connect_module

    async def _fake_clone_repo(owner_repo: str, name: str, *, github_token: str):
        dest = tmp_path / name
        (dest / "backend").mkdir(parents=True)
        (dest / "backend" / "requirements.txt").write_text("fastapi\n")
        (dest / "frontend").mkdir()
        (dest / "frontend" / "package.json").write_text("{}")
        return dest

    monkeypatch.setattr(repo_connect_module, "clone_repo", _fake_clone_repo)
    respx_mock.get("https://api.github.com/repos/acme/mono").mock(
        return_value=httpx.Response(200, json={"permissions": {"push": True}})
    )

    from unittest.mock import AsyncMock, MagicMock

    session = AsyncMock()
    no_existing_app = MagicMock()
    no_existing_app.scalar_one_or_none.return_value = None
    session.execute = AsyncMock(return_value=no_existing_app)

    with pytest.raises(SubprojectsFound) as exc_info:
        await repo_connect_module.connect_repo(
            session,
            repo_url="https://github.com/acme/mono",
            name="mono",
            github_token="fake-token",
        )
    rel_paths = {sp.rel_path for sp in exc_info.value.subprojects}
    assert rel_paths == {"backend", "frontend"}
    assert "backend" in str(exc_info.value)
    assert "frontend" in str(exc_info.value)


def test_select_subproject_repoints_an_apps_write_scope() -> None:
    """Uses the real CONNECTED_APPS_ROOT (not a monkeypatched tmp_path)
    because `select_subproject` calls `mcp_server.sandbox.to_repo_relative`,
    which resolves against the real `REPO_ROOT` -- patching only
    `core.repo_connect`'s own `REPO_ROOT` reference wouldn't reach it."""
    import shutil
    import uuid

    from core.models import MonitoredApp
    from core.repo_connect import CONNECTED_APPS_ROOT

    app_name = f"mono-{uuid.uuid4().hex[:12]}"
    clone_root = CONNECTED_APPS_ROOT / app_name
    (clone_root / "backend").mkdir(parents=True)
    (clone_root / "backend" / "requirements.txt").write_text("fastapi\n")

    try:
        app = MonitoredApp(
            name=app_name,
            language="unknown",
            local_repo_path=f"connected_apps/{app_name}",
            github_repo="acme/mono",
            allowed_write_paths=[f"connected_apps/{app_name}/"],
            test_command="pytest",
            ingest_token="fake-token",
            repo_url="https://github.com/acme/mono",
        )

        select_subproject(app, "backend")

        assert app.language == "python"
        assert app.local_repo_path == f"connected_apps/{app_name}/backend"
        assert app.allowed_write_paths == [f"connected_apps/{app_name}/backend/"]
        assert app.test_command == "pytest"
    finally:
        shutil.rmtree(clone_root, ignore_errors=True)


def test_select_subproject_rejects_a_path_outside_the_clone(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import core.repo_connect as repo_connect_module
    from core.models import MonitoredApp

    monkeypatch.setattr(repo_connect_module, "CONNECTED_APPS_ROOT", tmp_path)
    monkeypatch.setattr(repo_connect_module, "REPO_ROOT", tmp_path.parent)
    (tmp_path / "mono").mkdir()

    app = MonitoredApp(
        name="mono",
        language="unknown",
        local_repo_path=f"{tmp_path.name}/mono",
        github_repo="acme/mono",
        allowed_write_paths=[f"{tmp_path.name}/mono/"],
        test_command="pytest",
        ingest_token="fake-token",
        repo_url="https://github.com/acme/mono",
    )

    with pytest.raises(RepoConnectError, match="escapes the repo"):
        select_subproject(app, "../../etc")


def test_app_fingerprint_is_stable_and_distinguishes_message() -> None:
    fp1 = app_fingerprint(1, "ruff", "a.py", 10, "F401: unused import")
    fp2 = app_fingerprint(1, "ruff", "a.py", 10, "F401: unused import")
    fp3 = app_fingerprint(1, "ruff", "a.py", 10, "E501: line too long")
    assert fp1 == fp2
    assert fp1 != fp3


@pytest.mark.asyncio
async def test_check_repo_access_denied_gives_a_clear_actionable_message(respx_mock) -> None:
    respx_mock.get("https://api.github.com/repos/someone/private-repo").mock(
        return_value=httpx.Response(404, json={"message": "Not Found"})
    )
    with pytest.raises(RepoConnectError) as exc_info:
        await check_repo_access("someone/private-repo")
    message = str(exc_info.value)
    assert "GITHUB_TOKEN" in message
    assert "repository access list" in message


@pytest.mark.asyncio
async def test_check_repo_access_succeeds_when_reachable(respx_mock) -> None:
    respx_mock.get("https://api.github.com/repos/someone/public-repo").mock(
        return_value=httpx.Response(
            200, json={"full_name": "someone/public-repo", "permissions": {"push": True}}
        )
    )
    data = await check_repo_access("someone/public-repo")
    assert data["full_name"] == "someone/public-repo"


@pytest.mark.asyncio
async def test_check_repo_access_rejects_no_push_access(respx_mock) -> None:
    respx_mock.get("https://api.github.com/repos/someone/readonly-repo").mock(
        return_value=httpx.Response(
            200,
            json={
                "full_name": "someone/readonly-repo",
                "permissions": {"push": False, "pull": True},
            },
        )
    )
    with pytest.raises(RepoConnectError) as exc_info:
        await check_repo_access("someone/readonly-repo")
    message = str(exc_info.value)
    assert "does not have write access" in message
    assert "someone/readonly-repo" in message


@pytest.mark.asyncio
async def test_check_repo_access_rejects_missing_permissions_field(respx_mock) -> None:
    """No `permissions` object at all (e.g. an unauthenticated-shaped response)
    must fail closed, not be treated as implicit access."""
    respx_mock.get("https://api.github.com/repos/someone/no-perms-repo").mock(
        return_value=httpx.Response(200, json={"full_name": "someone/no-perms-repo"})
    )
    with pytest.raises(RepoConnectError, match="does not have write access"):
        await check_repo_access("someone/no-perms-repo")


@pytest.mark.asyncio
async def test_check_repo_access_rejects_owner_not_in_allowed_list(
    respx_mock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(config_settings, "allowed_repo_owners", "acme,someorg")
    with pytest.raises(RepoConnectError) as exc_info:
        await check_repo_access("someone/private-repo")
    message = str(exc_info.value)
    assert "ALLOWED_REPO_OWNERS" in message
    assert "someone" in message
    # Owner check happens before the GitHub call, so nothing was even mocked.
    assert not respx_mock.calls


@pytest.mark.asyncio
async def test_check_repo_access_allows_owner_in_allowed_list(
    respx_mock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(config_settings, "allowed_repo_owners", "acme,someone")
    respx_mock.get("https://api.github.com/repos/someone/public-repo").mock(
        return_value=httpx.Response(
            200, json={"full_name": "someone/public-repo", "permissions": {"push": True}}
        )
    )
    data = await check_repo_access("someone/public-repo")
    assert data["full_name"] == "someone/public-repo"

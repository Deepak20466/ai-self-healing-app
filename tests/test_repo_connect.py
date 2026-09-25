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
    app_fingerprint,
    check_repo_access,
    detect_stack,
    parse_github_url,
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

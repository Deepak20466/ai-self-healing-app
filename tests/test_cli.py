"""Tests for the `selfheal` terminal CLI (cli/), all HTTP mocked via respx --
no real pod, no real healer-pod process. `cli.config`'s state file is
redirected to a pytest tmp_path so these tests never touch the real user
config dir this CLI writes to outside tests.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest
from typer.testing import CliRunner

from cli import config as cli_config
from cli.main import app

runner = CliRunner()
BASE_URL = "http://127.0.0.1:8000"


@pytest.fixture(autouse=True)
def _isolated_config_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli_config, "config_dir", lambda: tmp_path)


def _log_in() -> None:
    cli_config.save_session(base_url=BASE_URL, token="fake-session-token")


def test_status_when_logged_out_and_pods_down() -> None:
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0
    assert "down" in result.stdout
    assert "not logged in" in result.stdout


def test_status_when_logged_in_shows_backend_chains(respx_mock: Any) -> None:
    _log_in()
    respx_mock.get(url__regex=r"http://127\.0\.0\.1:\d+/healthz").mock(
        return_value=httpx.Response(404)
    )
    respx_mock.get("http://127.0.0.1:8003/mcp").mock(return_value=httpx.Response(404))
    respx_mock.get(f"{BASE_URL}/api/auth/session").mock(
        return_value=httpx.Response(200, json={"username": "admin"})
    )
    respx_mock.get(f"{BASE_URL}/api/backends").mock(
        return_value=httpx.Response(
            200,
            json={
                "fix_chain": [
                    {
                        "name": "claude_cli",
                        "role": "fix",
                        "has_key": True,
                        "cooling_down_until": None,
                        "active": True,
                    },
                    {
                        "name": "groq_api",
                        "role": "fix",
                        "has_key": False,
                        "cooling_down_until": None,
                        "active": False,
                    },
                ],
                "chat_chain": [],
            },
        )
    )
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0, result.output
    assert "logged in" in result.output
    assert "claude_cli" in result.output
    assert "no key" in result.output


def test_login_success(respx_mock: Any) -> None:
    respx_mock.post(f"{BASE_URL}/api/auth/login").mock(
        return_value=httpx.Response(
            200,
            json={"status": "ok"},
            headers={"set-cookie": "selfheal_session=abc123; HttpOnly"},
        )
    )
    result = runner.invoke(app, ["login", "--url", BASE_URL], input="hunter2\n")
    assert result.exit_code == 0, result.stdout
    assert "Logged in" in result.stdout
    session = cli_config.load_session()
    assert session is not None
    assert session.token == "abc123"


def test_login_failure_prints_friendly_error(respx_mock: Any) -> None:
    respx_mock.post(f"{BASE_URL}/api/auth/login").mock(
        return_value=httpx.Response(401, json={"detail": "Invalid username or password"})
    )
    result = runner.invoke(app, ["login", "--url", BASE_URL], input="wrong\n")
    assert result.exit_code == 1
    assert "Invalid username or password" in result.output
    assert cli_config.load_session() is None


def test_login_connection_refused_is_friendly() -> None:
    result = runner.invoke(app, ["login", "--url", "http://127.0.0.1:1"], input="x\n")
    assert result.exit_code == 1
    assert "selfheal up" in result.output


def test_commands_without_login_say_run_login() -> None:
    result = runner.invoke(app, ["errors"])
    assert result.exit_code == 1
    assert "selfheal login" in result.output


def test_errors_lists_from_api(respx_mock: Any) -> None:
    _log_in()
    respx_mock.get(f"{BASE_URL}/api/errors").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "id": 1,
                    "exception_type": "ZeroDivisionError",
                    "file_path": "apps/target_app/bugs.py",
                    "line_number": 10,
                    "occurrence_count": 3,
                }
            ],
        )
    )
    result = runner.invoke(app, ["errors"])
    assert result.exit_code == 0, result.output
    assert "ZeroDivisionError" in result.output


def test_errors_json_output(respx_mock: Any) -> None:
    _log_in()
    respx_mock.get(f"{BASE_URL}/api/errors").mock(
        return_value=httpx.Response(200, json=[{"id": 1}])
    )
    result = runner.invoke(app, ["errors", "--json"])
    assert result.exit_code == 0
    assert '"id": 1' in result.output


def test_metrics_reaching_a_down_api_says_run_up() -> None:
    _log_in()
    result = runner.invoke(app, ["metrics"])
    assert result.exit_code == 1
    assert "selfheal up" in result.output


def test_apps_list(respx_mock: Any) -> None:
    _log_in()
    respx_mock.get(f"{BASE_URL}/api/apps").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "id": 5,
                    "name": "node_app",
                    "language": "javascript",
                    "health_score": 82,
                    "open_findings": 2,
                }
            ],
        )
    )
    result = runner.invoke(app, ["apps", "list"])
    assert result.exit_code == 0, result.output
    assert "node_app" in result.output


def test_bare_apps_defaults_to_list(respx_mock: Any) -> None:
    _log_in()
    respx_mock.get(f"{BASE_URL}/api/apps").mock(
        return_value=httpx.Response(200, json=[{"id": 5, "name": "node_app"}])
    )
    result = runner.invoke(app, ["apps"])
    assert result.exit_code == 0, result.output
    assert "node_app" in result.output


def test_apps_set_auto_merge(respx_mock: Any) -> None:
    _log_in()
    respx_mock.get(f"{BASE_URL}/api/apps").mock(
        return_value=httpx.Response(200, json=[{"id": 5, "name": "node_app"}])
    )
    patch_route = respx_mock.patch(f"{BASE_URL}/api/apps/5").mock(
        return_value=httpx.Response(200, json={"id": 5, "name": "node_app", "auto_merge": True})
    )
    result = runner.invoke(app, ["apps", "set", "node_app", "--auto-merge", "on"])
    assert result.exit_code == 0, result.output
    assert patch_route.called
    assert patch_route.calls.last.request.content == b'{"auto_merge":true}'


def test_apps_set_requires_a_value() -> None:
    result = runner.invoke(app, ["apps", "set", "node_app"])
    assert result.exit_code == 1


def test_scan_resolves_app_name_and_polls_until_scanned(respx_mock: Any) -> None:
    _log_in()
    respx_mock.get(f"{BASE_URL}/api/apps").mock(
        return_value=httpx.Response(200, json=[{"id": 5, "name": "node_app"}])
    )
    respx_mock.post(f"{BASE_URL}/api/apps/5/scan").mock(
        return_value=httpx.Response(200, json={"status": "scanning"})
    )
    respx_mock.get(f"{BASE_URL}/api/apps/5").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": 5,
                "name": "node_app",
                "health_score": 90,
                "last_scanned_at": "2026-01-01T00:00:00Z",
                "findings": [
                    {
                        "id": 11,
                        "severity": "high",
                        "file_path": "src/app.js",
                        "line_number": 5,
                        "message": "sql injection",
                    }
                ],
            },
        )
    )
    result = runner.invoke(app, ["scan", "node_app"])
    assert result.exit_code == 0, result.output
    assert "sql injection" in result.output


def test_fix_confirms_before_spending_ai_budget(respx_mock: Any) -> None:
    _log_in()
    respx_mock.get(f"{BASE_URL}/api/apps").mock(
        return_value=httpx.Response(200, json=[{"id": 5, "name": "node_app"}])
    )
    respx_mock.get(f"{BASE_URL}/api/apps/5").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": 5,
                "findings": [{"id": 11, "severity": "high"}],
            },
        )
    )
    fix_route = respx_mock.post(f"{BASE_URL}/api/findings/11/fix").mock(
        return_value=httpx.Response(200, json={"heal_job_id": 99, "finding_id": 11})
    )
    result = runner.invoke(app, ["fix", "node_app", "11"], input="n\n")
    assert result.exit_code == 0
    assert not fix_route.called

    result = runner.invoke(app, ["fix", "node_app", "11"], input="y\n")
    assert result.exit_code == 0, result.output
    assert fix_route.called
    assert "99" in result.output


def test_fix_yes_flag_skips_prompt(respx_mock: Any) -> None:
    _log_in()
    respx_mock.get(f"{BASE_URL}/api/apps").mock(
        return_value=httpx.Response(200, json=[{"id": 5, "name": "node_app"}])
    )
    respx_mock.get(f"{BASE_URL}/api/apps/5").mock(
        return_value=httpx.Response(
            200, json={"id": 5, "findings": [{"id": 11, "severity": "critical"}]}
        )
    )
    respx_mock.post(f"{BASE_URL}/api/findings/11/fix").mock(
        return_value=httpx.Response(200, json={"heal_job_id": 1, "finding_id": 11})
    )
    result = runner.invoke(app, ["fix", "node_app", "--high", "--yes"])
    assert result.exit_code == 0, result.output


def test_connect_posts_repo_url(respx_mock: Any) -> None:
    _log_in()
    respx_mock.post(f"{BASE_URL}/api/apps").mock(
        return_value=httpx.Response(200, json={"id": 7, "name": "my-repo", "status": "scanning"})
    )
    result = runner.invoke(app, ["connect", "https://github.com/owner/my-repo"])
    assert result.exit_code == 0, result.output
    assert "my-repo" in result.output


def test_prs_lists_recent_heal_job_prs(respx_mock: Any) -> None:
    _log_in()
    respx_mock.get(f"{BASE_URL}/api/prs").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "heal_job_id": 10,
                    "pr_number": 42,
                    "status": "pr_opened",
                    "pr_opened_at": "2026-01-01T00:00:00Z",
                }
            ],
        )
    )
    result = runner.invoke(app, ["prs"])
    assert result.exit_code == 0, result.output
    assert "42" in result.output


def test_capture_posts_to_capture_test_endpoint(respx_mock: Any) -> None:
    _log_in()
    respx_mock.get(f"{BASE_URL}/api/apps").mock(
        return_value=httpx.Response(200, json=[{"id": 5, "name": "node_app"}])
    )
    respx_mock.post(f"{BASE_URL}/api/apps/5/capture-test").mock(
        return_value=httpx.Response(
            200, json={"status": "captured", "error_id": 1, "fingerprint": "abc"}
        )
    )
    result = runner.invoke(app, ["capture", "node_app"])
    assert result.exit_code == 0, result.output
    assert "Captured" in result.output


def test_logout_clears_session(respx_mock: Any) -> None:
    _log_in()
    respx_mock.post(f"{BASE_URL}/api/auth/logout").mock(
        return_value=httpx.Response(200, json={"status": "ok"})
    )
    result = runner.invoke(app, ["logout"])
    assert result.exit_code == 0
    assert cli_config.load_session() is None


def test_up_down_delegate_to_pods_module(monkeypatch: pytest.MonkeyPatch) -> None:
    from cli import pods

    async def fake_start_pods() -> dict[str, str]:
        return {"app": "started", "sentinel": "started", "mcp": "started", "healer": "started"}

    async def fake_pod_is_up(_name: str) -> bool:
        return True

    monkeypatch.setattr(pods, "start_pods", fake_start_pods)
    monkeypatch.setattr(pods, "pod_is_up", fake_pod_is_up)
    result = runner.invoke(app, ["up"])
    assert result.exit_code == 0, result.output
    assert "All pods up." in result.output

    monkeypatch.setattr(pods, "stop_pods", lambda: ["app", "sentinel", "mcp", "healer"])
    result = runner.invoke(app, ["down"])
    assert result.exit_code == 0
    assert "Stopped" in result.output

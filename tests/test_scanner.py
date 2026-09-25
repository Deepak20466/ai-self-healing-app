"""Tests for core/scanner.py: pure output parsers, the health-score
heuristic, and the directory-containment guard (`_app_dir`) that keeps a
scan confined to its own `connected_apps/<name>/` directory.
"""

from __future__ import annotations

import json

import pytest

from core.models import FindingCategory, FindingSeverity, MonitoredApp
from core.repo_connect import CONNECTED_APPS_ROOT
from core.scanner import (
    FindingDraft,
    ScanError,
    _app_dir,
    compute_health_score,
    parse_mypy_json,
    parse_npm_audit_json,
    parse_pip_audit_json,
    parse_ruff_json,
)


def _app(local_repo_path: str, name: str = "testapp") -> MonitoredApp:
    return MonitoredApp(
        id=1,
        name=name,
        language="python",
        local_repo_path=local_repo_path,
        github_repo="acme/testapp",
        allowed_write_paths=[f"{local_repo_path}/"],
        test_command="pytest",
        ingest_token="x" * 32,
    )


def test_app_dir_rejects_a_path_outside_connected_apps_root() -> None:
    app = _app("apps/target_app")  # inside the repo, but not connected_apps/
    with pytest.raises(ScanError, match="outside connected_apps"):
        _app_dir(app)


def test_app_dir_rejects_traversal_out_of_connected_apps() -> None:
    app = _app("connected_apps/../../etc")
    with pytest.raises(ScanError):
        _app_dir(app)


def test_app_dir_accepts_a_real_directory_under_connected_apps(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("core.scanner.CONNECTED_APPS_ROOT", tmp_path)
    monkeypatch.setattr("core.scanner.REPO_ROOT", tmp_path.parent)
    app_dir = tmp_path / "testapp"
    app_dir.mkdir()
    app = _app(f"{tmp_path.name}/testapp")
    resolved = _app_dir(app)
    assert resolved == app_dir.resolve()


def test_app_dir_rejects_a_missing_directory() -> None:
    app = _app("connected_apps/does-not-exist-app")
    with pytest.raises(ScanError, match="does not exist"):
        _app_dir(app)
    assert not (CONNECTED_APPS_ROOT / "does-not-exist-app").exists()


def test_parse_ruff_json() -> None:
    output = json.dumps(
        [
            {
                "filename": "app/foo.py",
                "location": {"row": 12, "column": 1},
                "code": "F401",
                "message": "'os' imported but unused",
            }
        ]
    )
    drafts = parse_ruff_json(output)
    assert len(drafts) == 1
    assert drafts[0].category == FindingCategory.LINT
    assert drafts[0].tool == "ruff"
    assert drafts[0].file_path == "app/foo.py"
    assert drafts[0].line_number == 12
    assert "F401" in drafts[0].message


def test_parse_ruff_json_ignores_garbage() -> None:
    assert parse_ruff_json("not json") == []
    assert parse_ruff_json('{"not": "a list"}') == []


def test_parse_mypy_json() -> None:
    output = "\n".join(
        [
            "some non-json preamble line",
            json.dumps(
                {
                    "file": "app/foo.py",
                    "line": 5,
                    "severity": "error",
                    "message": "Incompatible return value type",
                }
            ),
            json.dumps(
                {"file": "app/bar.py", "line": 1, "severity": "note", "message": "see above"}
            ),
        ]
    )
    drafts = parse_mypy_json(output)
    assert len(drafts) == 2
    assert drafts[0].severity == FindingSeverity.MEDIUM
    assert drafts[1].severity == FindingSeverity.LOW
    assert all(d.category == FindingCategory.TYPE_CHECK for d in drafts)


def test_parse_pip_audit_json() -> None:
    output = json.dumps(
        {
            "dependencies": [
                {
                    "name": "requests",
                    "version": "2.25.0",
                    "vulns": [
                        {
                            "id": "GHSA-xxxx",
                            "fix_versions": ["2.31.0"],
                            "description": "some CVE",
                        }
                    ],
                },
                {"name": "flask", "version": "3.0.0", "vulns": []},
            ]
        }
    )
    drafts = parse_pip_audit_json(output)
    assert len(drafts) == 1
    assert drafts[0].category == FindingCategory.DEPENDENCY
    assert drafts[0].severity == FindingSeverity.HIGH
    assert "requests" in drafts[0].message
    assert "2.31.0" in drafts[0].message


def test_parse_npm_audit_json() -> None:
    output = json.dumps(
        {
            "vulnerabilities": {
                "lodash": {
                    "severity": "critical",
                    "via": [{"title": "Prototype Pollution"}],
                }
            }
        }
    )
    drafts = parse_npm_audit_json(output)
    assert len(drafts) == 1
    assert drafts[0].severity == FindingSeverity.CRITICAL
    assert "lodash" in drafts[0].message
    assert "Prototype Pollution" in drafts[0].message


def test_compute_health_score_perfect_when_no_findings_and_tests_pass() -> None:
    assert compute_health_score([], tests_passed=True) == 100


def test_compute_health_score_penalizes_by_severity_and_failing_tests() -> None:
    drafts = [
        FindingDraft(FindingCategory.LINT, FindingSeverity.LOW, "ruff", "msg"),
        FindingDraft(FindingCategory.DEPENDENCY, FindingSeverity.CRITICAL, "pip-audit", "msg"),
    ]
    score = compute_health_score(drafts, tests_passed=False)
    assert score == 100 - 2 - 20 - 20


def test_compute_health_score_never_goes_below_zero() -> None:
    drafts = [FindingDraft(FindingCategory.DEPENDENCY, FindingSeverity.CRITICAL, "x", "m")] * 20
    assert compute_health_score(drafts, tests_passed=False) == 0

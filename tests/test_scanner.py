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


async def test_run_scan_isolates_a_real_python_app_in_its_own_venv(tmp_path, monkeypatch) -> None:
    """Real, un-mocked integration test: creates a fresh per-app venv, installs
    the scanner's own tools into it, and runs a real pytest/ruff/mypy pass
    against a tiny fixture project. This is deliberately not just parser unit
    tests -- it's what actually caught a real bug (pytest itself was missing
    from `_SCANNER_TOOLS`, so every Python app's test run failed with "No
    module named pytest" even though its own dependencies installed fine).
    Slow (creates a real venv + installs 4 packages, ~60-120s) but worth it:
    mocking the subprocess calls would never have caught that bug.
    """
    import uuid

    from core.db import session_scope
    from core.scanner import run_scan

    # A real, committed row (session_scope(), not the rollback-wrapped
    # db_session fixture) in the shared test DB -- name and ingest_token are
    # both unique-constrained, so both must be randomized per run or a
    # leftover row from an earlier run collides with a UniqueViolationError
    # (hit this for real: a hardcoded "fixture-app" name did exactly that).
    unique = uuid.uuid4().hex[:12]
    app_name = f"fixture-app-{unique}"

    app_dir = tmp_path / app_name
    app_dir.mkdir()
    (app_dir / "requirements.txt").write_text("")
    (app_dir / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    (app_dir / "test_calc.py").write_text(
        "from calc import add\n\n\ndef test_add() -> None:\n    assert add(1, 2) == 3\n"
    )

    monkeypatch.setattr("core.scanner.CONNECTED_APPS_ROOT", tmp_path)
    monkeypatch.setattr("core.scanner.REPO_ROOT", tmp_path.parent)

    async with session_scope() as session:
        app = MonitoredApp(
            name=app_name,
            language="python",
            local_repo_path=f"{tmp_path.name}/{app_name}",
            github_repo=f"local/{app_name}",
            allowed_write_paths=[f"{tmp_path.name}/{app_name}/"],
            test_command="pytest",
            lint_command="ruff check .",
            ingest_token=uuid.uuid4().hex,
        )
        session.add(app)
        await session.flush()
        summary = await run_scan(session, app)
        app_id = app.id

    try:
        assert summary.tests_passed is True
        venv_python = app_dir / ".selfheal_venv" / "Scripts" / "python.exe"
        assert venv_python.exists()
    finally:
        async with session_scope() as session:
            row = await session.get(MonitoredApp, app_id)
            if row is not None:
                await session.delete(row)

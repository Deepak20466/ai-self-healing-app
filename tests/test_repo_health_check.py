"""Tests for core/repo_health_check.py: the static, read-only `selfheal
prepare` checklist -- never runs the app's own code, so every test here
just writes files to a tmp_path and asserts on the resulting PrepareReport.
"""

from __future__ import annotations

from core.repo_health_check import analyze_repo


def test_fully_missing_python_app_flags_everything(tmp_path) -> None:
    (tmp_path / "requirements.txt").write_text("fastapi\n")
    report = analyze_repo(tmp_path, app_name="x", language="python")
    assert report.has_tests is False
    assert report.has_ci_workflow is False
    assert report.already_fixable is False
    assert len(report.missing) >= 2


def test_a_well_prepared_python_app_has_nothing_missing(tmp_path) -> None:
    (tmp_path / "requirements.txt").write_text("fastapi\n")
    (tmp_path / "test_app.py").write_text("def test_x(): assert True\n")
    (tmp_path / ".github" / "workflows").mkdir(parents=True)
    (tmp_path / ".github" / "workflows" / "ci.yml").write_text(
        "name: CI\njobs:\n  test:\n    steps:\n      - run: pytest\n"
    )
    report = analyze_repo(tmp_path, app_name="x", language="python")
    assert report.has_tests is True
    assert report.test_file_count == 1
    assert report.has_ci_workflow is True
    assert report.ci_runs_tests is True
    assert report.already_fixable is True
    assert report.missing == ()


def test_ci_workflow_that_only_lints_does_not_count_as_running_tests(tmp_path) -> None:
    (tmp_path / "requirements.txt").write_text("fastapi\n")
    (tmp_path / "test_app.py").write_text("def test_x(): assert True\n")
    (tmp_path / ".github" / "workflows").mkdir(parents=True)
    (tmp_path / ".github" / "workflows" / "ci.yml").write_text(
        "name: CI\njobs:\n  lint:\n    steps:\n      - run: ruff check .\n"
    )
    report = analyze_repo(tmp_path, app_name="x", language="python")
    assert report.has_ci_workflow is True
    assert report.ci_runs_tests is False
    assert any("doesn't appear to run tests" in m for m in report.missing)


def test_external_service_dependency_without_test_config_is_flagged(tmp_path) -> None:
    (tmp_path / "requirements.txt").write_text("fastapi\npsycopg2-binary\n")
    (tmp_path / "test_app.py").write_text("def test_x(): assert True\n")
    (tmp_path / ".github" / "workflows").mkdir(parents=True)
    (tmp_path / ".github" / "workflows" / "ci.yml").write_text(
        "name: CI\njobs:\n  test:\n    steps:\n      - run: pytest\n"
    )
    report = analyze_repo(tmp_path, app_name="x", language="python")
    assert report.external_services == ("postgresql",)
    assert any("external service" in m for m in report.missing)


def test_external_service_dependency_with_env_example_is_not_flagged(tmp_path) -> None:
    (tmp_path / "requirements.txt").write_text("fastapi\npsycopg2-binary\n")
    (tmp_path / "test_app.py").write_text("def test_x(): assert True\n")
    (tmp_path / ".env.example").write_text("DATABASE_URL=postgresql://fake\n")
    (tmp_path / ".github" / "workflows").mkdir(parents=True)
    (tmp_path / ".github" / "workflows" / "ci.yml").write_text(
        "name: CI\njobs:\n  test:\n    steps:\n      - run: pytest\n"
    )
    report = analyze_repo(tmp_path, app_name="x", language="python")
    assert not any("external service" in m for m in report.missing)


def test_heavy_dependency_is_flagged(tmp_path) -> None:
    (tmp_path / "requirements.txt").write_text("torch>=2.0\n")
    report = analyze_repo(tmp_path, app_name="x", language="python")
    assert report.heavy_dependencies == ("torch",)
    assert any("Heavy dependencies" in m for m in report.missing)


def test_javascript_test_file_detection(tmp_path) -> None:
    (tmp_path / "package.json").write_text("{}")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.test.js").write_text("test('x', () => {});\n")
    report = analyze_repo(tmp_path, app_name="x", language="javascript")
    assert report.has_tests is True
    assert report.test_file_count == 1

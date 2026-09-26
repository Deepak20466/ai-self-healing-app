"""Onboarding PR picks the right error-capture setup per language."""

from __future__ import annotations

import pytest

from core.models import MonitoredApp
from healer.onboarding import build_onboarding_file, build_onboarding_pr_body


def _app(language: str) -> MonitoredApp:
    return MonitoredApp(
        name="demo",
        language=language,
        local_repo_path="connected_apps/demo",
        github_repo="o/r",
        allowed_write_paths=[],
        test_command="t",
        ingest_token="tok123",
    )


def test_python_keeps_the_middleware_helper() -> None:
    f = build_onboarding_file(_app("python"))
    assert f.path == "selfheal_error_reporter.py"
    assert "/ingest/error" in f.content and "tok123" in f.content


@pytest.mark.parametrize(
    ("language", "path"),
    [
        ("javascript", "selfheal_otel.js"),
        ("go", "selfheal_otel.go"),
        ("java", "selfheal-otel.properties"),
        ("csharp", "SelfHealOtel.cs"),
        ("php", "selfheal_otel.php"),
        ("ruby", "selfheal_otel.rb"),
    ],
)
def test_other_languages_get_official_otel_config(language: str, path: str) -> None:
    app = _app(language)
    f = build_onboarding_file(app)
    assert f.path == path
    assert "tok123" in f.content and "Bearer" in f.content
    assert "__" not in f.content  # every placeholder substituted
    assert "OpenTelemetry" in build_onboarding_pr_body(app, f)

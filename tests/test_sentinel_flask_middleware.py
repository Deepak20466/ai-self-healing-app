"""sentinel/flask_middleware.py: the Flask counterpart to SentinelMiddleware.

Uses a fake SyncSentinelClient (records reports, never a real HTTP call) --
same policy as this repo's other capture tests: no test opens a real socket.
"""

from __future__ import annotations

from flask import Flask

from sentinel.capture import CapturedError
from sentinel.flask_middleware import init_sentinel_flask


class _FakeSyncClient:
    def __init__(self) -> None:
        self.reported: list[CapturedError] = []

    def report_error(self, captured: CapturedError) -> None:
        self.reported.append(captured)

    def close(self) -> None:
        pass


def _make_app() -> tuple[Flask, _FakeSyncClient]:
    app = Flask(__name__)
    fake_client = _FakeSyncClient()
    init_sentinel_flask(app, sentinel_client=fake_client)

    @app.route("/boom")
    def boom() -> str:
        _ = 1 / 0
        return "unreachable"

    @app.route("/ok")
    def ok() -> str:
        return "fine"

    return app, fake_client


def test_flask_view_exception_is_captured() -> None:
    app, fake_client = _make_app()
    client = app.test_client()

    response = client.get("/boom")

    assert response.status_code == 500
    assert len(fake_client.reported) == 1
    captured = fake_client.reported[0]
    assert captured.exception_type == "ZeroDivisionError"
    assert captured.function_name == "boom"
    assert captured.request_context["path"] == "/boom"


def test_flask_successful_request_reports_nothing() -> None:
    app, fake_client = _make_app()
    client = app.test_client()

    response = client.get("/ok")

    assert response.status_code == 200
    assert fake_client.reported == []

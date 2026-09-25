"""sentinel/django_middleware.py: the Django counterpart to SentinelMiddleware.

Django's test tooling needs `django.conf.settings` configured before any
model/app machinery is touched -- done once here via `django.conf.settings.
configure()` (no real Django project/app needed for a plain middleware
class), then `django.setup()`.
"""

from __future__ import annotations

import django
from django.conf import settings as django_settings
from django.http import HttpRequest, HttpResponse
from django.test import RequestFactory

from sentinel import django_middleware
from sentinel.capture import CapturedError

if not django_settings.configured:
    django_settings.configure(DEBUG=False, ALLOWED_HOSTS=["*"])
    django.setup()


class _FakeSyncClient:
    def __init__(self) -> None:
        self.reported: list[CapturedError] = []

    def report_error(self, captured: CapturedError) -> None:
        self.reported.append(captured)

    def close(self) -> None:
        pass


def test_process_exception_reports_the_error() -> None:
    fake_client = _FakeSyncClient()

    def get_response(request: HttpRequest) -> HttpResponse:
        return HttpResponse("ok")

    middleware = django_middleware.SentinelDjangoMiddleware(get_response)
    middleware._client = fake_client

    request = RequestFactory().get("/boom")
    try:
        _ = 1 / 0
    except ZeroDivisionError as exc:
        middleware.process_exception(request, exc)

    assert len(fake_client.reported) == 1
    captured = fake_client.reported[0]
    assert captured.exception_type == "ZeroDivisionError"
    assert captured.request_context["path"] == "/boom"


def test_call_passes_through_to_get_response() -> None:
    def get_response(request: HttpRequest) -> HttpResponse:
        return HttpResponse("fine")

    middleware = django_middleware.SentinelDjangoMiddleware(get_response)
    request = RequestFactory().get("/ok")

    response = middleware(request)

    assert response.content == b"fine"

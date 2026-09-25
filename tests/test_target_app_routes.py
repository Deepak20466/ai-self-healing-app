"""HTTP-level checks that each /trigger/* route behaves as designed."""

from __future__ import annotations

import httpx
import pytest


async def test_healthz_is_public_and_ok(target_app_client: httpx.AsyncClient) -> None:
    response = await target_app_client.get("/healthz")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


@pytest.mark.parametrize(
    "path", ["/trigger/zero", "/trigger/key", "/trigger/none_lookup", "/trigger/validation"]
)
async def test_exception_triggers_return_500(
    target_app_client: httpx.AsyncClient, path: str
) -> None:
    response = await target_app_client.get(path)
    assert response.status_code == 500
    assert response.json() == {"detail": "Internal Server Error"}


async def test_trigger_timeout_returns_500(
    target_app_client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from apps.target_app import bugs

    monkeypatch.setattr(bugs.settings, "pricing_api_timeout_seconds", 0.3)
    response = await target_app_client.get("/trigger/timeout")
    assert response.status_code == 500


async def test_trigger_off_by_one_returns_200_with_wrong_content(
    target_app_client: httpx.AsyncClient,
) -> None:
    """Silent bug: a normal 200 response, just the wrong items."""
    response = await target_app_client.get("/trigger/off_by_one")
    assert response.status_code == 200
    ids = [item["id"] for item in response.json()["items"]]
    assert ids == [1, 5, 4]  # correct answer would be [3, 1, 5]


async def test_trigger_timezone_returns_200_with_the_now_fixed_date(
    target_app_client: httpx.AsyncClient,
) -> None:
    """This branch (PR #10) is the healer's own auto-fix for this bug — see
    test_target_app_bugs.py::test_bug5_timezone_delivery_estimate_is_now_fixed."""
    response = await target_app_client.get("/trigger/timezone")
    assert response.status_code == 200
    assert response.json()["estimated_delivery_date"] == "2026-01-05"

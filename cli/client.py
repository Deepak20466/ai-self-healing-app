"""Thin async HTTP client over the healer-pod JSON API for the `selfheal` CLI.

Every command that needs the API goes through `HealerClient`, which turns
the two most common failure modes into the friendly, actionable messages
the task asked for ("run selfheal up", "run selfheal login") instead of a
raw connection-refused traceback or a bare 401.
"""

from __future__ import annotations

from typing import Any

import httpx

from cli.config import Session, load_session
from healer.auth import SESSION_COOKIE_NAME


class CliError(Exception):
    """Base class for errors the CLI prints as a friendly one-liner, no traceback."""


class NotRunningError(CliError):
    def __init__(self) -> None:
        super().__init__("Can't reach the healer API. Run `selfheal up` first.")


class NotLoggedInError(CliError):
    def __init__(self) -> None:
        super().__init__("Not logged in (or your session expired). Run `selfheal login`.")


class ApiError(CliError):
    def __init__(self, status_code: int, detail: str) -> None:
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"API error {status_code}: {detail}")


def _require_session() -> Session:
    session = load_session()
    if session is None:
        raise NotLoggedInError
    return session


class HealerClient:
    """One short-lived instance per CLI invocation; not a long-running connection."""

    def __init__(self, session: Session) -> None:
        self._session = session
        self._client = httpx.AsyncClient(
            base_url=session.base_url,
            cookies={SESSION_COOKIE_NAME: session.token},
            timeout=30.0,
        )

    async def __aenter__(self) -> HealerClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self._client.aclose()

    @classmethod
    def from_saved_session(cls) -> HealerClient:
        return cls(_require_session())

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            resp = await self._client.request(method, path, **kwargs)
        except httpx.ConnectError as exc:
            raise NotRunningError from exc
        if resp.status_code == 401:
            raise NotLoggedInError
        if resp.status_code >= 400:
            detail = resp.text
            try:
                detail = resp.json().get("detail", detail)
            except Exception:
                pass
            raise ApiError(resp.status_code, str(detail))
        if resp.status_code == 204 or not resp.content:
            return None
        return resp.json()

    async def get(self, path: str, **kwargs: Any) -> Any:
        return await self._request("GET", path, **kwargs)

    async def post(self, path: str, **kwargs: Any) -> Any:
        return await self._request("POST", path, **kwargs)

    async def patch(self, path: str, **kwargs: Any) -> Any:
        return await self._request("PATCH", path, **kwargs)


async def healthz(base_url: str, timeout: float = 3.0) -> bool:
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(f"{base_url}/healthz")
            return resp.status_code == 200
    except httpx.HTTPError:
        return False

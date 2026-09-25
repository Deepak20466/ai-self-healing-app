"""healer/auth.py: password hashing/verification, session cookies, lockout."""

from __future__ import annotations

import pytest
from argon2 import PasswordHasher
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import settings as core_settings
from healer.auth import (
    AuthError,
    authenticate,
    issue_session_cookie,
    require_auth,
    verify_session_cookie,
)

PASSWORD = "correct horse battery staple"


@pytest.fixture(autouse=True)
def _configure_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(core_settings, "admin_password_hash", PasswordHasher().hash(PASSWORD))
    monkeypatch.setattr(core_settings, "session_secret", "test-session-secret")
    monkeypatch.setattr(core_settings, "login_max_attempts", 3)
    monkeypatch.setattr(core_settings, "login_lockout_minutes", 15)


def test_session_cookie_roundtrip() -> None:
    token = issue_session_cookie("admin")
    assert verify_session_cookie(token) == "admin"


def test_verify_session_cookie_rejects_missing_or_bad_token() -> None:
    with pytest.raises(AuthError):
        verify_session_cookie(None)
    with pytest.raises(AuthError):
        verify_session_cookie("not-a-real-token")


async def test_require_auth_raises_401_without_cookie() -> None:
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc_info:
        await require_auth(None)
    assert exc_info.value.status_code == 401


async def test_authenticate_succeeds_with_correct_password(db_session: AsyncSession) -> None:
    token = await authenticate(
        db_session, ip_address="10.0.0.1", username="admin", password=PASSWORD
    )
    assert verify_session_cookie(token) == "admin"


async def test_authenticate_fails_with_wrong_password(db_session: AsyncSession) -> None:
    with pytest.raises(AuthError):
        await authenticate(db_session, ip_address="10.0.0.2", username="admin", password="wrong")


async def test_authenticate_locks_out_after_max_failed_attempts(db_session: AsyncSession) -> None:
    ip = "10.0.0.3"
    for _ in range(3):
        with pytest.raises(AuthError):
            await authenticate(db_session, ip_address=ip, username="admin", password="wrong")

    with pytest.raises(AuthError, match="locked out"):
        await authenticate(db_session, ip_address=ip, username="admin", password=PASSWORD)

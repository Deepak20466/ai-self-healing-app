"""Single-admin-account auth for the chat/dashboard/metrics UI (Phase 6).

SPEC.md SECURITY: "Single admin account; the password is set in `.env` as
`ADMIN_PASSWORD_HASH` (argon2). Sessions use signed httpOnly, Secure,
SameSite=Strict cookies. Socket.io connections require a valid session
token." Login lockout/rate limiting reuse `core.ratelimit` (built in Phase 1,
unused until now since nothing needed login before Phase 6's UI existed).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from fastapi import Cookie, HTTPException, Request, status
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import settings
from core.db import get_db
from core.ratelimit import is_login_locked_out, record_login_attempt

SESSION_COOKIE_NAME = "selfheal_session"
SESSION_MAX_AGE_SECONDS = 12 * 60 * 60  # 12h
ADMIN_USERNAME = "admin"
_SALT = "selfheal-ui-session"

_hasher = PasswordHasher()


class AuthError(Exception):
    """Raised for bad credentials, lockout, or a missing/invalid session cookie."""


def _serializer() -> URLSafeTimedSerializer:
    if not settings.session_secret:
        raise AuthError("SESSION_SECRET is not configured")
    return URLSafeTimedSerializer(settings.session_secret, salt=_SALT)


def issue_session_cookie(username: str = ADMIN_USERNAME) -> str:
    """Sign a session token embedding the username and issue time."""
    payload = {"username": username, "issued_at": datetime.now(UTC).isoformat()}
    token: str = _serializer().dumps(payload)
    return token


def verify_session_cookie(token: str | None) -> str:
    """Return the username if `token` is a valid, unexpired session cookie."""
    if not token:
        raise AuthError("Not authenticated")
    try:
        data = _serializer().loads(token, max_age=SESSION_MAX_AGE_SECONDS)
    except SignatureExpired as exc:
        raise AuthError("Session expired") from exc
    except BadSignature as exc:
        raise AuthError("Invalid session") from exc
    username = data.get("username")
    if not isinstance(username, str):
        raise AuthError("Invalid session")
    return username


async def authenticate(
    session: AsyncSession, *, ip_address: str, username: str, password: str
) -> str:
    """Verify credentials against ADMIN_PASSWORD_HASH, enforcing lockout/rate limit.

    Returns a signed session cookie value on success. Raises AuthError otherwise.
    Every attempt (success or failure) is recorded in `login_attempts`.
    """
    if not settings.admin_password_hash:
        raise AuthError("ADMIN_PASSWORD_HASH is not configured")

    locked_out = await is_login_locked_out(
        session,
        ip_address=ip_address,
        username=username,
        max_attempts=settings.login_max_attempts,
        lockout_minutes=settings.login_lockout_minutes,
    )
    if locked_out:
        raise AuthError(
            f"Too many failed attempts; locked out for {settings.login_lockout_minutes} minutes"
        )

    success = False
    if username == ADMIN_USERNAME:
        try:
            _hasher.verify(settings.admin_password_hash, password)
            success = True
        except VerifyMismatchError:
            success = False
        except Exception:
            success = False

    await record_login_attempt(session, ip_address=ip_address, username=username, success=success)
    await session.commit()

    if not success:
        raise AuthError("Invalid username or password")
    return issue_session_cookie(username)


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


async def require_auth(
    selfheal_session: Annotated[str | None, Cookie()] = None,
) -> str:
    """FastAPI dependency: 401 unless a valid session cookie is present."""
    try:
        return verify_session_cookie(selfheal_session)
    except AuthError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc


def verify_socketio_token(token: str | None) -> str:
    """Used by the Socket.io `connect` handler; raises AuthError, not HTTPException."""
    return verify_session_cookie(token)


__all__ = [
    "SESSION_COOKIE_NAME",
    "SESSION_MAX_AGE_SECONDS",
    "AuthError",
    "authenticate",
    "client_ip",
    "get_db",
    "issue_session_cookie",
    "require_auth",
    "timedelta",
    "verify_session_cookie",
    "verify_socketio_token",
]

"""Confirmation tokens for destructive MCP tools.

SPEC.md SECURITY: "Destructive actions (rollback, cancel, merge) require an
authenticated session plus an explicit 'yes' confirmation in chat before the
tool runs." The chat layer (healer/chat.py, Phase 6) is what actually gates
on "yes" from an authenticated session; once it does, it calls
`issue_confirmation_token` and passes the result through to the tool call.
Verifying it here — in code, not just by asking the model nicely — is what
makes the guardrail real per SPEC.md's prompt-injection-defense section.
"""

from __future__ import annotations

from typing import Any

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from core.config import settings

DEFAULT_MAX_AGE_SECONDS = 300
_SALT = "mcp-destructive-action-confirmation"


class ConfirmationError(Exception):
    """Raised for a missing, expired, malformed, or mismatched-action token."""


def _serializer() -> URLSafeTimedSerializer:
    if not settings.session_secret:
        raise ConfirmationError("SESSION_SECRET is not configured")
    return URLSafeTimedSerializer(settings.session_secret, salt=_SALT)


def issue_confirmation_token(action: str, **payload: str) -> str:
    """Issue a signed, time-limited token binding `action` (+ any payload)."""
    data = {"action": action, **payload}
    token: str = _serializer().dumps(data)
    return token


def verify_confirmation_token(
    token: str, action: str, max_age_seconds: int = DEFAULT_MAX_AGE_SECONDS
) -> dict[str, Any]:
    """Verify `token` was issued for `action` and hasn't expired. Raises ConfirmationError."""
    if not token:
        raise ConfirmationError("A confirmation token is required for this action")
    try:
        data: dict[str, Any] = _serializer().loads(token, max_age=max_age_seconds)
    except SignatureExpired as exc:
        raise ConfirmationError("Confirmation token has expired") from exc
    except BadSignature as exc:
        raise ConfirmationError("Confirmation token is invalid") from exc

    if data.get("action") != action:
        raise ConfirmationError(f"Confirmation token is not valid for action {action!r}")
    return data

"""mcp_server.confirmation: signed, time-limited tokens for destructive tools."""

from __future__ import annotations

import pytest

from core.config import settings as core_settings
from mcp_server.confirmation import (
    ConfirmationError,
    issue_confirmation_token,
    verify_confirmation_token,
)


@pytest.fixture(autouse=True)
def _configure_session_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(core_settings, "session_secret", "test-session-secret")


def test_a_freshly_issued_token_verifies() -> None:
    token = issue_confirmation_token("trigger_rollback", env="production")
    data = verify_confirmation_token(token, "trigger_rollback")
    assert data["action"] == "trigger_rollback"
    assert data["env"] == "production"


def test_token_for_a_different_action_is_rejected() -> None:
    token = issue_confirmation_token("cancel_workflow")
    with pytest.raises(ConfirmationError):
        verify_confirmation_token(token, "trigger_rollback")


def test_tampered_token_is_rejected() -> None:
    token = issue_confirmation_token("trigger_rollback")
    tampered = token[:-1] + ("a" if token[-1] != "a" else "b")
    with pytest.raises(ConfirmationError):
        verify_confirmation_token(tampered, "trigger_rollback")


def test_expired_token_is_rejected() -> None:
    token = issue_confirmation_token("trigger_rollback")
    # -1 guarantees "expired" regardless of clock/second-resolution timing,
    # unlike max_age_seconds=0 which can race against itsdangerous's
    # 1-second timestamp resolution.
    with pytest.raises(ConfirmationError):
        verify_confirmation_token(token, "trigger_rollback", max_age_seconds=-1)


def test_empty_token_is_rejected() -> None:
    with pytest.raises(ConfirmationError):
        verify_confirmation_token("", "trigger_rollback")


def test_missing_session_secret_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(core_settings, "session_secret", None)
    with pytest.raises(ConfirmationError):
        issue_confirmation_token("trigger_rollback")

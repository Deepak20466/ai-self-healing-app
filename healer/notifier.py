"""Optional, lightweight notifications (SPEC.md NOTIFICATIONS).

Sends fix/deploy/rollback/anomaly/budget events to Slack (webhook) and/or
email (SMTP) when configured in `.env`; skips silently otherwise. Also used
as the chat-facing "push a message into every open chat session" mechanism
via `broadcast()`, which `healer/app.py` wires to Socket.io — this is what
lets `scripts/local_deploy.py` (Phase 7) prove a rollback "notifies the
chat" without a human watching a terminal.
"""

from __future__ import annotations

import smtplib
from collections.abc import Awaitable, Callable
from email.message import EmailMessage

import httpx
import structlog

from core.config import settings

logger = structlog.get_logger(__name__)

# Set by healer/app.py at startup to broadcast a Socket.io "notification"
# event to every connected, authenticated client. None outside a running
# healer-pod process (e.g. plain worker/tests), in which case broadcast() is
# a no-op — notifications never crash the caller.
_socket_broadcaster: Callable[[str, dict[str, object]], Awaitable[None]] | None = None


def set_socket_broadcaster(fn: Callable[[str, dict[str, object]], Awaitable[None]] | None) -> None:
    global _socket_broadcaster
    _socket_broadcaster = fn


async def notify(event: str, message: str, **extra: object) -> None:
    """Fire-and-forget-ish notification: Socket.io broadcast + Slack + email.

    Never raises — monitoring/notification must never break the caller
    (same principle as sentinel's capture path swallowing transport errors).
    """
    payload: dict[str, object] = {"event": event, "message": message, **extra}

    if _socket_broadcaster is not None:
        try:
            await _socket_broadcaster("notification", payload)
        except Exception:
            logger.warning("notifier.socket_broadcast_failed", event=event)

    if settings.slack_webhook_url:
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                await client.post(settings.slack_webhook_url, json={"text": f"[{event}] {message}"})
        except Exception:
            logger.warning("notifier.slack_failed", event=event)

    if settings.smtp_host and settings.smtp_to and settings.smtp_from:
        try:
            _send_email(subject=f"[selfheal] {event}", body=message)
        except Exception:
            logger.warning("notifier.smtp_failed", event=event)


def _send_email(*, subject: str, body: str) -> None:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = settings.smtp_from
    msg["To"] = settings.smtp_to
    msg.set_content(body)

    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=5) as smtp:
        smtp.starttls()
        if settings.smtp_user and settings.smtp_password:
            smtp.login(settings.smtp_user, settings.smtp_password)
        smtp.send_message(msg)


__all__ = ["notify", "set_socket_broadcaster"]

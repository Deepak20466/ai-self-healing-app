"""Redact secrets and PII before anything is persisted (SPEC.md sentinel-pod
section) or sent to a third-party AI backend (terminal-only v1.0 Step 5 --
see `mcp_server/audit.py`, the privacy-guard choke point every AI backend's
tool calls pass through).

Two layers, both defense-in-depth:
  - `_SENSITIVE_KEY_PATTERN`: any dict key that *looks* sensitive (password,
    token, secret, authorization, cookie, ...) has its value replaced
    outright, regardless of what the value looks like.
  - Regex patterns over free text (tracebacks, log lines, messages) redact
    emails, bearer/API tokens, connection-string credentials, this project's
    own AI backend key formats, and common secret-key=value patterns that
    might appear embedded in a message or stack frame's local variables.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

_REDACTED = "[REDACTED]"

_SENSITIVE_KEY_PATTERN = re.compile(
    r"(password|passwd|secret|token|api[_-]?key|authorization|cookie|session[_-]?id|ssn|"
    r"credit[_-]?card)",
    re.IGNORECASE,
)

_EMAIL_PATTERN = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_BEARER_TOKEN_PATTERN = re.compile(r"\bBearer\s+[A-Za-z0-9\-._~+/]+=*", re.IGNORECASE)
_GITHUB_TOKEN_PATTERN = re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b")
_GENERIC_ANTHROPIC_KEY_PATTERN = re.compile(r"\bsk-ant-[A-Za-z0-9\-_]{20,}\b")
# This project's own free-tier AI backend key formats (terminal-only v1.0
# Step 5) -- these get scrubbed from MCP tool results too (see
# mcp_server/audit.py), since a job's own captured error/log text could, in
# principle, contain one of these keys verbatim (e.g. an app that logs its
# own misconfigured environment) and that text is what gets sent on to
# whichever third-party AI backend is driving the fix.
_GROQ_KEY_PATTERN = re.compile(r"\bgsk_[A-Za-z0-9]{20,}\b")
_GOOGLE_API_KEY_PATTERN = re.compile(r"\bAIzaSy[A-Za-z0-9_-]{20,}\b")
_GOOGLE_AUTH_KEY_PATTERN = re.compile(r"\bAQ\.[A-Za-z0-9._-]{25,}\b")
# user:password@host in a connection string (postgresql://, redis://, amqp://, ...).
_CONN_STRING_CREDENTIALS_PATTERN = re.compile(r"(?i)\b(\w+://)[^\s:/@]+:[^\s@/]+@")
_KEY_VALUE_SECRET_PATTERN = re.compile(
    r"(?i)\b(password|passwd|secret|token|api[_-]?key)\b\s*[:=]\s*\S+"
)
_CREDIT_CARD_PATTERN = re.compile(r"\b(?:\d[ -]*?){13,16}\b")

_Replacement = str | Callable[[re.Match[str]], str]
_TEXT_PATTERNS: tuple[tuple[re.Pattern[str], _Replacement], ...] = (
    (_BEARER_TOKEN_PATTERN, "Bearer " + _REDACTED),
    (_GITHUB_TOKEN_PATTERN, _REDACTED),
    (_GENERIC_ANTHROPIC_KEY_PATTERN, _REDACTED),
    (_GROQ_KEY_PATTERN, _REDACTED),
    (_GOOGLE_API_KEY_PATTERN, _REDACTED),
    (_GOOGLE_AUTH_KEY_PATTERN, _REDACTED),
    (_CONN_STRING_CREDENTIALS_PATTERN, lambda m: f"{m.group(1)}{_REDACTED}@"),
    (_KEY_VALUE_SECRET_PATTERN, lambda m: f"{m.group(1)}={_REDACTED}"),
    (_EMAIL_PATTERN, "[REDACTED_EMAIL]"),
    (_CREDIT_CARD_PATTERN, "[REDACTED_CARD]"),
)


def scrub_text(text: str) -> str:
    """Redact secrets/PII in free text (tracebacks, log messages, CI output)."""
    scrubbed = text
    for pattern, replacement in _TEXT_PATTERNS:
        scrubbed = pattern.sub(replacement, scrubbed)
    return scrubbed


def scrub_value(value: Any) -> Any:
    if isinstance(value, str):
        return scrub_text(value)
    if isinstance(value, dict):
        return scrub_dict(value)
    if isinstance(value, list):
        return [scrub_value(item) for item in value]
    return value


def scrub_dict(data: dict[str, Any]) -> dict[str, Any]:
    """Recursively scrub a dict: sensitive-named keys are wiped outright."""
    scrubbed: dict[str, Any] = {}
    for key, value in data.items():
        if _SENSITIVE_KEY_PATTERN.search(key):
            scrubbed[key] = _REDACTED
        else:
            scrubbed[key] = scrub_value(value)
    return scrubbed

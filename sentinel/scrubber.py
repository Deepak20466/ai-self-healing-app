"""Redact secrets and PII before anything is persisted (SPEC.md sentinel-pod section).

Two layers, both defense-in-depth:
  - `_SENSITIVE_KEY_PATTERN`: any dict key that *looks* sensitive (password,
    token, secret, authorization, cookie, ...) has its value replaced
    outright, regardless of what the value looks like.
  - Regex patterns over free text (tracebacks, log lines, messages) redact
    emails, bearer/API tokens, and common secret-key=value patterns that
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
_KEY_VALUE_SECRET_PATTERN = re.compile(
    r"(?i)\b(password|passwd|secret|token|api[_-]?key)\b\s*[:=]\s*\S+"
)
_CREDIT_CARD_PATTERN = re.compile(r"\b(?:\d[ -]*?){13,16}\b")

_Replacement = str | Callable[[re.Match[str]], str]
_TEXT_PATTERNS: tuple[tuple[re.Pattern[str], _Replacement], ...] = (
    (_BEARER_TOKEN_PATTERN, "Bearer " + _REDACTED),
    (_GITHUB_TOKEN_PATTERN, _REDACTED),
    (_GENERIC_ANTHROPIC_KEY_PATTERN, _REDACTED),
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

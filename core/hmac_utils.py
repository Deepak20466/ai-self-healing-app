"""HMAC signing/verification for inbound webhooks (SPEC.md SECURITY section).

Signature scheme is deliberately similar to Stripe's: `t=<unix_ts>,v1=<hex_hmac>`
over the string `f"{ts}.{body}"`, so the signature is bound to a timestamp and
replay protection is a simple window check on that timestamp.
"""

from __future__ import annotations

import hashlib
import hmac as hmac_lib
import time

SIGNATURE_SCHEME_VERSION = "v1"


class InvalidSignatureError(Exception):
    """Raised when a webhook signature is missing, malformed, or does not match."""


def sign_payload(payload: bytes, secret: str, timestamp: int | None = None) -> str:
    """Return a `t=...,v1=...` signature header for `payload`."""
    ts = timestamp if timestamp is not None else int(time.time())
    signed_string = f"{ts}.".encode() + payload
    digest = hmac_lib.new(secret.encode("utf-8"), signed_string, hashlib.sha256).hexdigest()
    return f"t={ts},{SIGNATURE_SCHEME_VERSION}={digest}"


def _parse_signature_header(header: str) -> tuple[int, str]:
    parts = dict(item.split("=", 1) for item in header.split(",") if "=" in item)
    if "t" not in parts or SIGNATURE_SCHEME_VERSION not in parts:
        raise InvalidSignatureError("Signature header missing required fields")
    try:
        ts = int(parts["t"])
    except ValueError as exc:
        raise InvalidSignatureError("Signature header has a non-numeric timestamp") from exc
    return ts, parts[SIGNATURE_SCHEME_VERSION]


def verify_signature(
    payload: bytes,
    signature_header: str,
    secret: str,
    tolerance_seconds: int = 300,
    now: int | None = None,
) -> None:
    """Verify `signature_header` over `payload`.

    Raises `InvalidSignatureError` if the signature is malformed, does not
    match, or falls outside the replay-protection window. Never returns a
    partial/boolean result — callers must treat any exception as "reject".
    """
    if not signature_header:
        raise InvalidSignatureError("Missing signature header")

    ts, provided_digest = _parse_signature_header(signature_header)

    current_time = now if now is not None else int(time.time())
    if abs(current_time - ts) > tolerance_seconds:
        raise InvalidSignatureError("Signature timestamp outside replay-protection window")

    expected_signed_string = f"{ts}.".encode() + payload
    expected_digest = hmac_lib.new(
        secret.encode("utf-8"), expected_signed_string, hashlib.sha256
    ).hexdigest()

    if not hmac_lib.compare_digest(expected_digest, provided_digest):
        raise InvalidSignatureError("Signature does not match payload")

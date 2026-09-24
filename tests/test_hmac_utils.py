"""Unit tests for core.hmac_utils: webhook signing and replay protection."""

from __future__ import annotations

import pytest

from core.hmac_utils import InvalidSignatureError, sign_payload, verify_signature

SECRET = "test-secret"


def test_valid_signature_is_accepted() -> None:
    payload = b'{"event": "workflow_run"}'
    header = sign_payload(payload, SECRET, timestamp=1_000_000)

    verify_signature(payload, header, SECRET, now=1_000_005)


def test_tampered_payload_is_rejected() -> None:
    payload = b'{"event": "workflow_run"}'
    header = sign_payload(payload, SECRET, timestamp=1_000_000)

    with pytest.raises(InvalidSignatureError):
        verify_signature(b'{"event": "tampered"}', header, SECRET, now=1_000_005)


def test_wrong_secret_is_rejected() -> None:
    payload = b'{"event": "workflow_run"}'
    header = sign_payload(payload, SECRET, timestamp=1_000_000)

    with pytest.raises(InvalidSignatureError):
        verify_signature(payload, header, "wrong-secret", now=1_000_005)


def test_replayed_old_timestamp_is_rejected() -> None:
    payload = b'{"event": "workflow_run"}'
    header = sign_payload(payload, SECRET, timestamp=1_000_000)

    with pytest.raises(InvalidSignatureError):
        verify_signature(payload, header, SECRET, tolerance_seconds=300, now=1_000_600)


def test_missing_signature_header_is_rejected() -> None:
    with pytest.raises(InvalidSignatureError):
        verify_signature(b"{}", "", SECRET)


def test_malformed_signature_header_is_rejected() -> None:
    with pytest.raises(InvalidSignatureError):
        verify_signature(b"{}", "not-a-valid-header", SECRET)

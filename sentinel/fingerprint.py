"""Dedup fingerprints for errors and contract violations.

Error fingerprints hash the exception type plus the *normalized* top frames
(relative file path + function name — deliberately excluding line numbers,
which shift as unrelated code changes) so the same bug keeps the same
fingerprint across commits.
"""

from __future__ import annotations

import hashlib


def fingerprint_error(exception_type: str, file_path: str, function_name: str) -> str:
    """Fingerprint from the exception type and the single responsible frame.

    Callers pass the already-selected "best" frame (see
    `sentinel.capture.build_captured_error`), which is what actually
    identifies a distinct bug for our purposes — the exact same
    (exception_type, file, function) triple recurring is the same bug.
    """
    normalized_path = file_path.replace("\\", "/")
    digest_input = f"{exception_type}:{normalized_path}:{function_name}"
    return hashlib.sha256(digest_input.encode("utf-8")).hexdigest()[:32]


def fingerprint_contract_violation(endpoint: str, case_name: str) -> str:
    """Fingerprint for a contract violation: stable per (endpoint, case)."""
    digest_input = f"contract:{endpoint}:{case_name}"
    return hashlib.sha256(digest_input.encode("utf-8")).hexdigest()[:32]

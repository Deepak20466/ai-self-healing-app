"""Unit tests for sentinel.fingerprint: dedup hashing."""

from __future__ import annotations

from sentinel.fingerprint import fingerprint_contract_violation, fingerprint_error


def test_same_inputs_produce_the_same_fingerprint() -> None:
    a = fingerprint_error("ZeroDivisionError", "apps/target_app/bugs.py", "average_rating")
    b = fingerprint_error("ZeroDivisionError", "apps/target_app/bugs.py", "average_rating")
    assert a == b


def test_different_exception_types_produce_different_fingerprints() -> None:
    a = fingerprint_error("ZeroDivisionError", "apps/target_app/bugs.py", "average_rating")
    b = fingerprint_error("KeyError", "apps/target_app/bugs.py", "average_rating")
    assert a != b


def test_windows_and_posix_path_separators_produce_the_same_fingerprint() -> None:
    a = fingerprint_error("KeyError", "apps/target_app/bugs.py", "order_status_label")
    b = fingerprint_error("KeyError", "apps\\target_app\\bugs.py", "order_status_label")
    assert a == b


def test_contract_violation_fingerprint_is_stable_per_endpoint_and_case() -> None:
    a = fingerprint_contract_violation("/items/top", "top_items_by_rating")
    b = fingerprint_contract_violation("/items/top", "top_items_by_rating")
    assert a == b


def test_contract_violation_fingerprint_differs_by_case_name() -> None:
    a = fingerprint_contract_violation("/items/top", "top_items_by_rating")
    b = fingerprint_contract_violation("/items/top", "a_different_case")
    assert a != b

"""mcp_server.log_trim: trimming a raw CI log to the failing step, capped at 20KB."""

from __future__ import annotations

from mcp_server.log_trim import trim_to_failing_step


def test_extracts_the_group_containing_the_error() -> None:
    log = (
        "2026-01-01T00:00:00Z ##[group]Run ruff\n"
        "2026-01-01T00:00:01Z ruff output, all clean\n"
        "2026-01-01T00:00:02Z ##[endgroup]\n"
        "2026-01-01T00:00:03Z ##[group]Run pytest\n"
        "2026-01-01T00:00:04Z FAILED tests/test_x.py::test_y\n"
        "2026-01-01T00:00:05Z ##[error]Process completed with exit code 1.\n"
        "2026-01-01T00:00:06Z ##[endgroup]\n"
        "2026-01-01T00:00:07Z ##[group]Upload artifacts\n"
        "2026-01-01T00:00:08Z done\n"
    )
    trimmed = trim_to_failing_step(log)

    assert "Run pytest" in trimmed
    assert "FAILED tests/test_x.py::test_y" in trimmed
    assert "##[error]" in trimmed
    assert "Run ruff" not in trimmed
    assert "Upload artifacts" not in trimmed


def test_falls_back_to_context_around_error_without_group_markers() -> None:
    log = "\n".join([f"line {i}" for i in range(300)] + ["##[error]boom"])
    trimmed = trim_to_failing_step(log)

    assert "##[error]boom" in trimmed
    assert "line 0" not in trimmed  # too far before the error, outside the context window


def test_falls_back_to_whole_log_without_any_error_marker() -> None:
    log = "just some normal output\nnothing failed\n"
    assert trim_to_failing_step(log) == log


def test_caps_output_at_max_bytes_keeping_the_tail() -> None:
    log = "x" * 50_000 + "##[error]the actual failure"
    trimmed = trim_to_failing_step(log, max_bytes=100)

    assert len(trimmed.encode("utf-8")) <= 100
    assert trimmed.endswith("##[error]the actual failure")


def test_uses_the_last_error_when_multiple_groups_fail() -> None:
    log = (
        "##[group]Step one\n"
        "##[error]first failure\n"
        "##[endgroup]\n"
        "##[group]Step two\n"
        "##[error]second failure\n"
        "##[endgroup]\n"
    )
    trimmed = trim_to_failing_step(log)

    assert "Step two" in trimmed
    assert "second failure" in trimmed
    assert "Step one" not in trimmed

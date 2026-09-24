"""Trim a raw GitHub Actions job log to the failing step, capped at 20KB.

SPEC.md's `get_job_logs` tool: "fetch logs, trim them to the failing step,
cap at 20 KB." GitHub's raw logs mark each step with `##[group]<name>` and
mark failures within a step with `##[error]...`.
"""

from __future__ import annotations

DEFAULT_MAX_BYTES = 20_000
_FALLBACK_CONTEXT_LINES = 100


def trim_to_failing_step(log_text: str, max_bytes: int = DEFAULT_MAX_BYTES) -> str:
    """Return the failing step's log section (or a tail fallback), capped at `max_bytes`."""
    lines = log_text.splitlines()
    group_start_indices = [i for i, line in enumerate(lines) if "##[group]" in line]
    error_indices = [i for i, line in enumerate(lines) if "##[error]" in line]

    if group_start_indices and error_indices:
        trimmed = _extract_failing_group(lines, group_start_indices, error_indices)
    elif error_indices:
        start = max(0, error_indices[-1] - _FALLBACK_CONTEXT_LINES)
        trimmed = "\n".join(lines[start:])
    else:
        trimmed = log_text

    return _cap_bytes(trimmed, max_bytes)


def _extract_failing_group(
    lines: list[str], group_start_indices: list[int], error_indices: list[int]
) -> str:
    last_error_index = error_indices[-1]
    group_start = group_start_indices[0]
    for start in group_start_indices:
        if start <= last_error_index:
            group_start = start
        else:
            break
    later_groups = [s for s in group_start_indices if s > group_start]
    group_end = later_groups[0] if later_groups else len(lines)
    return "\n".join(lines[group_start:group_end])


def _cap_bytes(text: str, max_bytes: int) -> str:
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    return encoded[-max_bytes:].decode("utf-8", errors="ignore")

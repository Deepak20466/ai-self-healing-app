"""Build a `CapturedError` from a live exception.

This is the wire schema for `POST /ingest/error`: `build_captured_error` runs
inside the monitored app's process (where the traceback object actually
lives) and produces a JSON-serializable payload; sentinel-pod's ingest
endpoint validates incoming bodies against this same `CapturedError` model.
"""

from __future__ import annotations

import subprocess
import traceback
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import BaseModel

_REPO_ROOT = Path(__file__).resolve().parents[1]
# Frames from our own app code are far more useful than frames deep inside a
# third-party library (e.g. httpx internals for the timeout bug) — prefer
# the deepest frame that's actually "ours".
_IN_APP_MARKERS = ("apps/target_app", "apps\\target_app")


class CapturedError(BaseModel):
    exception_type: str
    message: str
    traceback: str
    file_path: str
    line_number: int
    function_name: str
    request_context: dict[str, Any] = {}
    git_sha: str | None = None
    occurred_at: datetime


@lru_cache
def get_git_sha() -> str | None:
    """Best-effort current commit SHA, cached for the life of the process."""
    try:
        result = subprocess.run(  # noqa: S603
            ["git", "rev-parse", "HEAD"],  # noqa: S607
            cwd=_REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
        return result.stdout.strip()
    except (subprocess.SubprocessError, OSError):
        return None


def _select_frame(
    exc: BaseException, in_app_markers: tuple[str, ...] = _IN_APP_MARKERS
) -> traceback.FrameSummary:
    frames = traceback.extract_tb(exc.__traceback__)
    if not frames:
        return traceback.FrameSummary(filename="<unknown>", lineno=0, name="<unknown>")

    for frame in reversed(frames):
        normalized = frame.filename.replace("\\", "/")
        if any(marker.replace("\\", "/") in normalized for marker in in_app_markers):
            return frame
    return frames[-1]


def _relative_path(raw_path: str) -> str:
    try:
        return Path(raw_path).resolve().relative_to(_REPO_ROOT).as_posix()
    except ValueError:
        return Path(raw_path).as_posix()


def build_captured_error(
    exc: BaseException,
    request_context: dict[str, Any] | None = None,
    *,
    in_app_markers: tuple[str, ...] | None = None,
) -> CapturedError:
    """Build the wire payload for an exception, pinpointing the responsible frame.

    `in_app_markers` (multi-app support) lets a non-target_app middleware
    (Flask/Django integrations, or a future registered app under a
    different directory) prefer its own in-app frames instead of
    target_app's -- defaults to the original target_app markers so existing
    behavior is unchanged when omitted.
    """
    frame = _select_frame(exc, in_app_markers or _IN_APP_MARKERS)
    formatted_traceback = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))

    return CapturedError(
        exception_type=type(exc).__name__,
        message=str(exc),
        traceback=formatted_traceback,
        file_path=_relative_path(frame.filename),
        line_number=frame.lineno or 0,
        function_name=frame.name,
        request_context=request_context or {},
        git_sha=get_git_sha(),
        occurred_at=datetime.now(UTC),
    )

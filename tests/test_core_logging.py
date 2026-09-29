"""Unit test for core.logging's global credential-scrubbing processor.

A real credentialed git remote URL (embedded in an unhandled exception's
message) reached healer.log verbatim -- this is the global defense-in-depth
layer added on top of scrubbing at the specific call site that leaked it
(see healer/worktree.py's `_run_git`).
"""

from __future__ import annotations

from core.logging import _scrub_event_dict


def test_scrub_event_dict_redacts_a_credentialed_url_in_the_event_message() -> None:
    token = "ghp_totallyFakeTokenForThisTestOnly1234"
    event_dict = {
        "event": f"git push failed: https://x-access-token:{token}@github.com/a/b.git denied",
        "exception": f"Traceback...\nWorktreeError: ...{token}...",
    }

    scrubbed = _scrub_event_dict(None, "error", event_dict)

    assert token not in scrubbed["event"]
    assert token not in scrubbed["exception"]
    assert "[REDACTED]" in scrubbed["event"]

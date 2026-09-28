"""healer/backend_chain.py: cooldown state, has_key_for, and chain selection.
Pure/in-memory logic, no DB, no network.
"""

from __future__ import annotations

import pytest

from core.config import settings as core_settings
from healer import backend_chain


@pytest.fixture(autouse=True)
def _clear_cooldowns() -> None:
    backend_chain.clear_cooldowns()
    yield
    backend_chain.clear_cooldowns()


def test_has_key_for_cli_backends_is_always_true() -> None:
    assert backend_chain.has_key_for("claude_cli") is True
    assert backend_chain.has_key_for("codex_cli") is True
    assert backend_chain.has_key_for("gemini_cli") is True


def test_has_key_for_api_backends_reflects_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(core_settings, "gemini_api_key", None)
    monkeypatch.setattr(core_settings, "groq_api_key", "gsk_x")
    monkeypatch.setattr(core_settings, "anthropic_api_key", None)
    assert backend_chain.has_key_for("gemini_api") is False
    assert backend_chain.has_key_for("groq_api") is True
    assert backend_chain.has_key_for("openrouter_api") is False
    monkeypatch.setattr(core_settings, "anthropic_api_key", "sk-or-x")
    assert backend_chain.has_key_for("openrouter_api") is True


def test_mark_cooldown_and_is_cooling_down() -> None:
    assert backend_chain.is_cooling_down("groq_api") is False
    backend_chain.mark_cooldown("groq_api", retry_after_seconds=3600)
    assert backend_chain.is_cooling_down("groq_api") is True
    assert backend_chain.cooldown_until("groq_api") is not None


def test_cooldown_expires_and_clears_itself() -> None:
    backend_chain.mark_cooldown("groq_api", retry_after_seconds=-1)  # already expired
    assert backend_chain.is_cooling_down("groq_api") is False
    assert backend_chain.cooldown_until("groq_api") is None


def test_first_eligible_skips_no_key_and_cooling_down(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(core_settings, "gemini_api_key", None)
    monkeypatch.setattr(core_settings, "groq_api_key", "gsk_x")
    chain = ["gemini_api", "groq_api", "claude_cli"]

    assert backend_chain.first_eligible(chain) == "groq_api"

    backend_chain.mark_cooldown("groq_api")
    assert backend_chain.first_eligible(chain) == "claude_cli"


def test_first_eligible_returns_to_first_choice_once_it_recovers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(core_settings, "groq_api_key", "gsk_x")
    chain = ["groq_api", "claude_cli"]
    backend_chain.mark_cooldown("groq_api", retry_after_seconds=-1)
    assert backend_chain.first_eligible(chain) == "groq_api"


def test_next_eligible_after_skips_already_tried(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(core_settings, "gemini_api_key", "g")
    monkeypatch.setattr(core_settings, "groq_api_key", "gsk_x")
    chain = ["claude_cli", "gemini_api", "groq_api"]
    assert backend_chain.next_eligible_after(chain, {"claude_cli"}) == "gemini_api"
    assert (
        backend_chain.next_eligible_after(chain, {"claude_cli", "gemini_api", "groq_api"}) is None
    )


def test_chain_states_reports_role_and_activity(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(core_settings, "groq_api_key", "gsk_x")
    backend_chain.mark_cooldown("groq_api")
    states = backend_chain.chain_states(["groq_api", "claude_cli"], role="fix")
    by_name = {s.name: s for s in states}
    assert by_name["groq_api"].has_key is True
    assert by_name["groq_api"].active is False
    assert by_name["claude_cli"].active is True
    assert all(s.role == "fix" for s in states)

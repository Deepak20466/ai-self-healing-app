"""Unit tests for core.config: defaults and env-driven overrides."""

from __future__ import annotations

from decimal import Decimal

from core.config import Settings


def test_defaults_are_sane_for_local_dev() -> None:
    s = Settings(_env_file=None)

    assert s.db_pool_size == 5
    assert s.db_max_overflow == 2
    assert s.anthropic_model == "claude-sonnet-5"
    assert s.auto_merge is False
    assert s.daily_budget_usd == Decimal("2.00")
    assert s.chat_daily_budget_usd == Decimal("1.00")
    assert s.max_tokens_per_job == 150_000


def test_env_overrides_are_applied(monkeypatch) -> None:
    monkeypatch.setenv("AUTO_MERGE", "true")
    monkeypatch.setenv("DAILY_BUDGET_USD", "5.50")
    monkeypatch.setenv("ANTHROPIC_MODEL", "claude-opus-5")

    s = Settings(_env_file=None)

    assert s.auto_merge is True
    assert s.daily_budget_usd == Decimal("5.50")
    assert s.anthropic_model == "claude-opus-5"

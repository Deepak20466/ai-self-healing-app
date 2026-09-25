"""healer.costs: token usage -> USD, per current Anthropic pricing."""

from __future__ import annotations

from decimal import Decimal

from healer.costs import TokenUsage, compute_cost_usd


def test_sonnet5_input_and_output_pricing() -> None:
    usage = TokenUsage(input_tokens=1_000_000, output_tokens=1_000_000)
    cost = compute_cost_usd("claude-sonnet-5", usage)
    assert cost == Decimal("12.000000")  # $2/Mtok input + $10/Mtok output


def test_cache_read_is_cheaper_than_fresh_input() -> None:
    fresh = compute_cost_usd("claude-sonnet-5", TokenUsage(input_tokens=1000, output_tokens=0))
    cached = compute_cost_usd(
        "claude-sonnet-5",
        TokenUsage(input_tokens=0, output_tokens=0, cache_read_input_tokens=1000),
    )
    assert Decimal("0") < cached < fresh


def test_cache_write_costs_more_than_fresh_input() -> None:
    fresh = compute_cost_usd("claude-sonnet-5", TokenUsage(input_tokens=1000, output_tokens=0))
    written = compute_cost_usd(
        "claude-sonnet-5",
        TokenUsage(input_tokens=0, output_tokens=0, cache_creation_input_tokens=1000),
    )
    assert written > fresh


def test_unknown_model_falls_back_to_sonnet5_pricing() -> None:
    usage = TokenUsage(input_tokens=1_000_000, output_tokens=500_000)
    assert compute_cost_usd("some-future-model", usage) == compute_cost_usd(
        "claude-sonnet-5", usage
    )


def test_zero_usage_is_zero_cost() -> None:
    usage = TokenUsage(input_tokens=0, output_tokens=0)
    assert compute_cost_usd("claude-sonnet-5", usage) == Decimal("0")

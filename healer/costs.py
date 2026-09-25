"""Claude API cost calculation from an Anthropic `usage` object.

Pricing is per-model and changes over time, so it's kept as one small table
here rather than scattered across call sites. Rates are USD per million
tokens (input / output / cache-write / cache-read), current as of this
writing; update `_PRICING` if `ANTHROPIC_MODEL` changes to a model not listed
— it falls back to Sonnet-5 rates (logged) rather than raising, since a
missing price entry should degrade to "probably close enough" cost tracking,
not break the healer loop.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

import structlog

logger = structlog.get_logger(__name__)

_MILLION = Decimal("1_000_000")


@dataclass(frozen=True)
class ModelPricing:
    input_per_mtok: Decimal
    output_per_mtok: Decimal
    cache_write_per_mtok: Decimal
    cache_read_per_mtok: Decimal


_PRICING: dict[str, ModelPricing] = {
    "claude-sonnet-5": ModelPricing(
        input_per_mtok=Decimal("2.00"),
        output_per_mtok=Decimal("10.00"),
        cache_write_per_mtok=Decimal("2.50"),
        cache_read_per_mtok=Decimal("0.20"),
    ),
    "claude-opus-5": ModelPricing(
        input_per_mtok=Decimal("5.00"),
        output_per_mtok=Decimal("25.00"),
        cache_write_per_mtok=Decimal("6.25"),
        cache_read_per_mtok=Decimal("0.50"),
    ),
    "claude-haiku-4-5": ModelPricing(
        input_per_mtok=Decimal("1.00"),
        output_per_mtok=Decimal("5.00"),
        cache_write_per_mtok=Decimal("1.25"),
        cache_read_per_mtok=Decimal("0.10"),
    ),
}

_DEFAULT_PRICING = _PRICING["claude-sonnet-5"]


@dataclass(frozen=True)
class TokenUsage:
    """Mirrors the fields of `anthropic.types.Usage` this module needs."""

    input_tokens: int
    output_tokens: int
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0


def compute_cost_usd(model: str, usage: TokenUsage) -> Decimal:
    """Total USD cost of one `messages.create` call, all four token kinds included.

    `fix_attempts.cached_tokens` only stores `cache_read_input_tokens` (the
    schema has one "cached" column, not separate read/write columns — see
    CLAUDE.md), but the cache-write tokens still cost real money, so this
    function folds them into `cost_usd` even though they aren't separately
    tracked as a column.
    """
    pricing = _PRICING.get(model)
    if pricing is None:
        logger.warning("costs.unknown_model_pricing", model=model, fallback="claude-sonnet-5")
        pricing = _DEFAULT_PRICING

    cost = (
        Decimal(usage.input_tokens) * pricing.input_per_mtok
        + Decimal(usage.output_tokens) * pricing.output_per_mtok
        + Decimal(usage.cache_creation_input_tokens) * pricing.cache_write_per_mtok
        + Decimal(usage.cache_read_input_tokens) * pricing.cache_read_per_mtok
    ) / _MILLION
    return cost.quantize(Decimal("0.000001"))

"""Per-model prices for cost accounting (USD per million tokens).

Only models whose prices are known are listed; anything else is costed at 0
and flagged, never guessed. Add rows or pass `prices=` to override.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(frozen=True)
class Price:
    input: float          # uncached input
    output: float
    cache_read: float     # cached input
    cache_write: float    # input written to cache (Anthropic charges a premium)


PRICES: dict[str, Price] = {
    # Anthropic first-party list prices (Claude API skill, cached 2026-10-06).
    # Cache writes are costed at 1.25x input (5-minute TTL).
    "claude-opus-5-5": Price(4.00, 20.00, 0.20, 5.00),
    "claude-sonnet-5-5": Price(2.00, 10.00, 0.20, 2.50),
    "claude-fable-5-1": Price(10.00, 50.00, 0.25, 12.50),
    # Local inference has no per-token price.
    "local": Price(0.0, 0.0, 0.0, 0.0),
}


def cost_usd(model: str, usage: Mapping[str, Any], prices: Mapping[str, Price] | None = None) -> tuple[float, bool]:
    """Return (cost, known). `usage.input_tokens` is total prompt tokens including cached ones."""
    table = prices or PRICES
    price = table.get(model)
    if price is None:
        return 0.0, False
    total_in = int(usage.get("input_tokens") or 0)
    cached = int(usage.get("cached_tokens") or 0)
    written = int(usage.get("cache_write_tokens") or 0)
    uncached = max(total_in - cached - written, 0)
    out = int(usage.get("output_tokens") or 0)
    cost = (
        uncached * price.input + cached * price.cache_read + written * price.cache_write + out * price.output
    ) / 1_000_000
    return round(cost, 6), True

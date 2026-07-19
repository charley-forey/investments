"""Anthropic token-cost estimation and per-cycle accounting.

Pricing is per 1M tokens (input / output). Cache reads are billed at ~0.1x input.
Update the table if pricing changes; unknown models fall back to Opus-tier rates.
"""

from __future__ import annotations

from dataclasses import dataclass

# $ per 1M tokens: (input, output)
PRICING: dict[str, tuple[float, float]] = {
    "claude-opus-4-8": (5.0, 25.0),
    "claude-opus-4-7": (5.0, 25.0),
    "claude-fable-5": (10.0, 50.0),
    "claude-sonnet-5": (3.0, 15.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
}
_DEFAULT = (5.0, 25.0)
_CACHE_READ_MULT = 0.1


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0

    def add(self, other: "Usage") -> None:
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cache_read_tokens += other.cache_read_tokens


def usage_from_response(response) -> Usage:
    """Extract token usage from an Anthropic response, tolerant of missing fields."""
    u = getattr(response, "usage", None)
    if u is None:
        return Usage()
    return Usage(
        input_tokens=int(getattr(u, "input_tokens", 0) or 0),
        output_tokens=int(getattr(u, "output_tokens", 0) or 0),
        cache_read_tokens=int(getattr(u, "cache_read_input_tokens", 0) or 0),
    )


def estimate_cost(usage: Usage, model: str) -> float:
    in_rate, out_rate = PRICING.get(model, _DEFAULT)
    # Non-cached input is billed at full rate; cache reads at the reduced multiple.
    billable_input = max(0, usage.input_tokens - usage.cache_read_tokens)
    cost = (
        billable_input * in_rate / 1_000_000
        + usage.cache_read_tokens * in_rate * _CACHE_READ_MULT / 1_000_000
        + usage.output_tokens * out_rate / 1_000_000
    )
    return round(cost, 6)

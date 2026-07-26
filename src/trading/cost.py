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
_CACHE_WRITE_MULT = 1.25  # 5-minute TTL; 1h TTL would be 2.0


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    def add(self, other: "Usage") -> None:
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cache_read_tokens += other.cache_read_tokens
        self.cache_write_tokens += other.cache_write_tokens


def usage_from_response(response) -> Usage:
    """Extract token usage from an Anthropic response, tolerant of missing fields."""
    u = getattr(response, "usage", None)
    if u is None:
        return Usage()
    return Usage(
        input_tokens=int(getattr(u, "input_tokens", 0) or 0),
        output_tokens=int(getattr(u, "output_tokens", 0) or 0),
        cache_read_tokens=int(getattr(u, "cache_read_input_tokens", 0) or 0),
        cache_write_tokens=int(getattr(u, "cache_creation_input_tokens", 0) or 0),
    )


def split_cost(usage: Usage, model: str) -> tuple[float, float]:
    """(input_cost, output_cost). Which half is bigger decides which lever is worth
    pulling — prompt/tool trimming or thinking depth — and the split moves a lot
    with model and effort, so it is worth reading rather than assuming.

    The three input counts are disjoint: the API reports `input_tokens` as the
    uncached remainder, so total prompt = input + cache_write + cache_read. Cache
    writes bill at 1.25x input and are the easiest cost to miss entirely — every
    cycle writes the cached system prompt."""
    in_rate, out_rate = PRICING.get(model, _DEFAULT)
    input_cost = (
        usage.input_tokens * in_rate
        + usage.cache_write_tokens * in_rate * _CACHE_WRITE_MULT
        + usage.cache_read_tokens * in_rate * _CACHE_READ_MULT
    ) / 1_000_000
    return input_cost, usage.output_tokens * out_rate / 1_000_000


def estimate_cost(usage: Usage, model: str) -> float:
    return round(sum(split_cost(usage, model)), 6)

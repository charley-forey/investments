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
    # OpenAI GPT-5.6, rates supplied 2026-07-31.
    "gpt-5.6-luna": (0.20, 1.20),
    "gpt-5.6-terra": (2.00, 12.00),
    "gpt-5.6-sol": (5.00, 30.00),
}
_DEFAULT = (5.0, 25.0)
_CACHE_READ_MULT = 0.1
_CACHE_WRITE_MULT = 1.25   # 5-minute TTL
_CACHE_WRITE_1H_MULT = 2.0  # 1-hour TTL, used for the frozen system+tools prefix

# OpenAI prices cached input as an explicit rate rather than a multiple of input,
# and -- the structurally important part -- charges NOTHING to populate the cache.
# Anthropic's write premium was $7.61 of a $13.74 day on 2026-07-30, so this is
# most of the difference between the providers, not the headline token rates.
_OPENAI_CACHED_INPUT: dict[str, float] = {
    "gpt-5.6-luna": 0.02,
    "gpt-5.6-terra": 0.20,
    "gpt-5.6-sol": 0.50,
}


def provider_for(model: str) -> str:
    """'openai' | 'anthropic'. Model id is the only routing signal there is."""
    return "openai" if (model or "").startswith("gpt-") else "anthropic"

# Models that accept `thinking={"type": "adaptive"}`. Older models require
# {"type": "enabled", "budget_tokens": N} and return 400 on adaptive -- which is
# exactly how the market digest died silently for six days (intel resolved to
# claude-haiku-4-5 while sending adaptive). Unknown models get NO thinking rather
# than a guess: a missing thinking block degrades an answer, a 400 loses it.
ADAPTIVE_THINKING_MODELS = frozenset({
    "claude-opus-5", "claude-opus-4-8", "claude-opus-4-7", "claude-opus-4-6",
    "claude-fable-5", "claude-mythos-5", "claude-sonnet-5", "claude-sonnet-4-6",
})


def supports_adaptive_thinking(model: str) -> bool:
    return model in ADAPTIVE_THINKING_MODELS

# Anthropic server-side web_search: $10 per 1,000 searches, billed on top of tokens.
# Invisible to the token ledger, so it used to be spent entirely off-book — 72
# strategy sessions on 2026-07-28 could each run up to `web_search_max_uses`.
#
# UNVERIFIED FOR OPENAI. Their server-side web_search is also billed per call, but
# the rate was not supplied with the token pricing, so searches on a gpt-* model
# are currently charged at Anthropic's rate. That is a placeholder, not a fact:
# it keeps the spend on-book rather than invisible, which is the failure that
# matters, but the number will be wrong until someone checks. Intel runs 2-5
# searches per digest, so the error is bounded at a few cents a day.
WEB_SEARCH_USD = 0.01


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0        # 5-minute TTL writes
    cache_write_1h_tokens: int = 0     # 1-hour TTL writes, billed at 2x not 1.25x
    # Already counted inside output_tokens and billed as output. Tracked separately
    # because it is the number that decides a provider comparison: gpt-5.6-luna
    # spent 516 of 602 output tokens reasoning on a probe where terra spent 107,
    # and whether that matters depends entirely on the per-token price.
    reasoning_tokens: int = 0
    web_searches: int = 0

    def add(self, other: "Usage") -> None:
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cache_read_tokens += other.cache_read_tokens
        self.cache_write_tokens += other.cache_write_tokens
        self.cache_write_1h_tokens += other.cache_write_1h_tokens
        self.reasoning_tokens += other.reasoning_tokens
        self.web_searches += other.web_searches


def usage_from_response(response) -> Usage:
    """Extract token usage from an Anthropic response, tolerant of missing fields."""
    u = getattr(response, "usage", None)
    if u is None:
        return Usage()
    # `cache_creation_input_tokens` is the TTL-blind total. When the per-TTL
    # breakdown is present, split it out so the 1h prefix bills at its real 2x
    # rather than being under-reported at the 5m rate.
    total_write = int(getattr(u, "cache_creation_input_tokens", 0) or 0)
    breakdown = getattr(u, "cache_creation", None)
    write_1h = int(getattr(breakdown, "ephemeral_1h_input_tokens", 0) or 0) if breakdown else 0
    return Usage(
        input_tokens=int(getattr(u, "input_tokens", 0) or 0),
        output_tokens=int(getattr(u, "output_tokens", 0) or 0),
        cache_read_tokens=int(getattr(u, "cache_read_input_tokens", 0) or 0),
        cache_write_tokens=max(0, total_write - write_1h),
        cache_write_1h_tokens=write_1h,
    )


def split_cost(usage: Usage, model: str) -> tuple[float, float]:
    """(input_cost, output_cost). Which half is bigger decides which lever is worth
    pulling — prompt/tool trimming or thinking depth — and the split moves a lot
    with model and effort, so it is worth reading rather than assuming.

    The three input counts are disjoint: the API reports `input_tokens` as the
    uncached remainder, so total prompt = input + cache_write + cache_read. Cache
    writes bill at 1.25x input and are the easiest cost to miss entirely — every
    cycle writes the cached system prompt.

    OpenAI bills the same three counts on a different shape: cached input has its
    own published rate rather than a multiple of input, and populating the cache
    is FREE. So `cache_write_tokens` there is simply ordinary input -- not a
    premium -- which is why the provider gap is much wider than the headline token
    rates suggest.
    """
    in_rate, out_rate = PRICING.get(model, _DEFAULT)
    if provider_for(model) == "openai":
        cached_rate = _OPENAI_CACHED_INPUT.get(model, in_rate * _CACHE_READ_MULT)
        input_cost = (
            (usage.input_tokens
             + usage.cache_write_tokens
             + usage.cache_write_1h_tokens) * in_rate
            + usage.cache_read_tokens * cached_rate
        ) / 1_000_000
        # Reasoning tokens are already inside output_tokens on the Responses API
        # and are billed as output; they are tracked separately for visibility.
        return input_cost, usage.output_tokens * out_rate / 1_000_000
    input_cost = (
        usage.input_tokens * in_rate
        + usage.cache_write_tokens * in_rate * _CACHE_WRITE_MULT
        + usage.cache_write_1h_tokens * in_rate * _CACHE_WRITE_1H_MULT
        + usage.cache_read_tokens * in_rate * _CACHE_READ_MULT
    ) / 1_000_000
    return input_cost, usage.output_tokens * out_rate / 1_000_000


def tool_cost(usage: Usage) -> float:
    """Server-side tool charges. Not a token lever, so deliberately kept out of
    split_cost -- but it is real money and must reach the daily cap."""
    return usage.web_searches * WEB_SEARCH_USD


def estimate_cost(usage: Usage, model: str) -> float:
    return round(sum(split_cost(usage, model)) + tool_cost(usage), 6)

"""Portfolio risk analytics: Value-at-Risk, Expected Shortfall, realized
volatility, and correlation from price history (no external data feed needed —
correlation is computed from the persisted bars). These feed sizing and the
concentration guardrails."""

from __future__ import annotations

import math
from dataclasses import dataclass


def daily_returns(closes: list[float]) -> list[float]:
    return [(closes[i] - closes[i - 1]) / closes[i - 1]
            for i in range(1, len(closes)) if closes[i - 1] > 0]


def annualized_vol(closes: list[float], periods: int = 252) -> float:
    rets = daily_returns(closes)
    if len(rets) < 2:
        return 0.0
    m = sum(rets) / len(rets)
    var = sum((r - m) ** 2 for r in rets) / (len(rets) - 1)
    return math.sqrt(var) * math.sqrt(periods)


def value_at_risk(returns: list[float], confidence: float = 0.95) -> float:
    """Historical VaR: the loss (positive number) not exceeded with `confidence`.
    e.g. 0.95 -> the 5th-percentile daily loss."""
    if not returns:
        return 0.0
    ordered = sorted(returns)
    idx = int((1.0 - confidence) * len(ordered))
    idx = min(max(idx, 0), len(ordered) - 1)
    return abs(min(ordered[idx], 0.0))


def expected_shortfall(returns: list[float], confidence: float = 0.95) -> float:
    """Average loss in the tail beyond VaR (a.k.a. CVaR)."""
    if not returns:
        return 0.0
    ordered = sorted(returns)
    cutoff = max(1, int((1.0 - confidence) * len(ordered)))
    tail = ordered[:cutoff]
    return abs(sum(tail) / len(tail)) if tail else 0.0


def correlation(a: list[float], b: list[float]) -> float:
    """Pearson correlation of two return series (aligned to the shorter length)."""
    n = min(len(a), len(b))
    if n < 2:
        return 0.0
    a, b = a[-n:], b[-n:]
    ma, mb = sum(a) / n, sum(b) / n
    cov = sum((a[i] - ma) * (b[i] - mb) for i in range(n))
    va = sum((x - ma) ** 2 for x in a)
    vb = sum((x - mb) ** 2 for x in b)
    denom = math.sqrt(va * vb)
    return cov / denom if denom else 0.0


@dataclass
class ConcentrationCheck:
    max_correlation: float
    correlated_with: str | None


def max_correlation_to_holdings(
    candidate_closes: list[float], holdings_closes: dict[str, list[float]]
) -> ConcentrationCheck:
    """Highest absolute return-correlation between a candidate and current holdings —
    used to stop the book from becoming several names that are really one bet."""
    cand = daily_returns(candidate_closes)
    best = 0.0
    who = None
    for symbol, closes in holdings_closes.items():
        c = abs(correlation(cand, daily_returns(closes)))
        if c > best:
            best, who = c, symbol
    return ConcentrationCheck(max_correlation=round(best, 3), correlated_with=who)

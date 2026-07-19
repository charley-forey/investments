"""Reference signal functions for the backtester. These are deliberately simple
baselines the agent's proposed strategies can be measured against."""

from __future__ import annotations

from .engine import Bar


def sma_crossover(fast: int = 10, slow: int = 30):
    """Long when the fast SMA is above the slow SMA, flat otherwise."""

    def signal(bars: list[Bar], i: int) -> int:
        if i < slow:
            return 0
        fast_avg = sum(b.close for b in bars[i - fast + 1:i + 1]) / fast
        slow_avg = sum(b.close for b in bars[i - slow + 1:i + 1]) / slow
        return 1 if fast_avg > slow_avg else 0

    return signal


def breakout(lookback: int = 20):
    """Long when the close makes a new `lookback`-day high, flat on a new low."""

    def signal(bars: list[Bar], i: int) -> int:
        if i < lookback:
            return 0
        window = bars[i - lookback:i]
        high = max(b.close for b in window)
        low = min(b.close for b in window)
        if bars[i].close >= high:
            return 1
        if bars[i].close <= low:
            return 0
        return 1 if bars[i].close > (high + low) / 2 else 0

    return signal

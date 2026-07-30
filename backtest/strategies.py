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


def _sma(bars: list[Bar], i: int, n: int) -> float:
    return sum(b.close for b in bars[i - n + 1:i + 1]) / n


def trend_pullback_long(fast: int = 20, slow: int = 50):
    """Uptrend, dip to the fast SMA, reclaim it. Long until the trend breaks.

    Worth validating first: it is the best template in the shadow ledger through
    2026-07-27 (+$264.66 on 4 wins in 5) and five samples is not evidence."""

    def signal(bars: list[Bar], i: int) -> int:
        if i < slow + 1:
            return 0
        close = bars[i].close
        if close < _sma(bars, i, slow):          # trend gone -> flat
            return 0
        fast_now, fast_prev = _sma(bars, i, fast), _sma(bars, i - 1, fast)
        reclaimed = bars[i - 1].close < fast_prev and close > fast_now
        return 1 if (reclaimed or close > fast_now) else 0

    return signal


def extended_from_sma(lookback: int = 20, threshold: float = 0.036):
    """Long while price is stretched above its `lookback` SMA, flat when it reverts.

    This is what the scanner branch formerly called "orb-breakout" actually computed
    — `range_break` is distance from the 20d SMA scaled by relative volume, on daily
    bars, with no opening range anywhere in it. Encoded here so the name finally has
    a signal behind it and the sweep can say whether it is worth trading at all.

    The 3.6% default is where the old scanner branch tripped: `range_break >= 0.6`
    with rvol >= 2 needs `abs(dist20) - 0.01 >= 0.03`."""

    def signal(bars: list[Bar], i: int) -> int:
        if i < lookback:
            return 0
        sma = _sma(bars, i, lookback)
        if sma <= 0:
            return 0
        return 1 if (bars[i].close - sma) / sma >= threshold else 0

    return signal


def momentum_continuation(lookback: int = 20, threshold: float = 0.05):
    """Long strong momentum, short weak. Two-sided, so it exercises the short path."""

    def signal(bars: list[Bar], i: int) -> int:
        if i < lookback:
            return 0
        past = bars[i - lookback].close
        if past <= 0:
            return 0
        ret = (bars[i].close - past) / past
        if ret >= threshold:
            return 1
        if ret <= -threshold:
            return -1
        return 0

    return signal


def _stdev(bars: list[Bar], i: int, n: int) -> float:
    window = [b.close for b in bars[i - n + 1:i + 1]]
    if len(window) < 2:
        return 0.0
    mean = sum(window) / len(window)
    return (sum((x - mean) ** 2 for x in window) / (len(window) - 1)) ** 0.5


def mean_reversion(lookback: int = 20, entry_z: float = -1.5, exit_z: float = 0.0):
    """Buy statistical stretch BELOW the mean; flat once it reverts.

    The registry's other rules are all long-vol trend bets, which is why they share
    a regime signature and lose together in quiet tape: a 2% stop grazes on noise
    and sells moves that never needed exiting. This is the opposite bet -- it wants
    price to come BACK, so it should earn where they bleed (up/calm, sideways/calm),
    which are the cells with no positive strategy at all.

    Long-only by construction: shorting stretch in a market with upward drift is
    fighting the drift, and the short tags already measure -0.53 in up/calm.
    """

    def signal(bars: list[Bar], i: int) -> int:
        if i < lookback:
            return 0
        sma = _sma(bars, i, lookback)
        sd = _stdev(bars, i, lookback)
        if sd <= 0 or sma <= 0:
            return 0
        z = (bars[i].close - sma) / sd
        if z <= entry_z:
            return 1
        if z >= exit_z:
            return 0
        return 1 if bars[i - 1].close < _sma(bars, i - 1, lookback) else 0

    return signal


def trend_pullback_short(fast: int = 20, slow: int = 50):
    """Mirror of `trend_pullback_long`: downtrend, rally into the fast SMA, fail.

    Short until the downtrend breaks. Registered so the bearish side is measurable
    rather than merely assertable — the system was long-only in practice and the
    regime sweep says these signals pay in `down/elevated`, which a long-only book
    cannot express at all.
    """

    def signal(bars: list[Bar], i: int) -> int:
        if i < slow + 1:
            return 0
        close = bars[i].close
        if close > _sma(bars, i, slow):           # downtrend gone -> flat
            return 0
        fast_now, fast_prev = _sma(bars, i, fast), _sma(bars, i - 1, fast)
        rejected = bars[i - 1].close > fast_prev and close < fast_now
        return -1 if (rejected or close < fast_now) else 0

    return signal


def breakdown(lookback: int = 20):
    """Mirror of `breakout`: short a new `lookback`-day low, flat on a new high."""

    def signal(bars: list[Bar], i: int) -> int:
        if i < lookback:
            return 0
        window = bars[i - lookback:i]
        high = max(b.close for b in window)
        low = min(b.close for b in window)
        if bars[i].close <= low:
            return -1
        if bars[i].close >= high:
            return 0
        return -1 if bars[i].close < (high + low) / 2 else 0

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

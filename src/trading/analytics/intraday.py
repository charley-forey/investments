"""Intraday feature computation from minute/interval bars — the same consistent
feature shape as analytics.features, but for the intraday tape: VWAP, opening
range, breakout, momentum, and relative volume. Pure math, no external data;
degrades to None when there aren't enough bars to say anything."""

from __future__ import annotations

from dataclasses import dataclass, asdict


@dataclass
class IntradayFeatures:
    last: float
    vwap: float                 # volume-weighted average price over the session
    opening_range_high: float   # high of the first `or_bars` bars
    opening_range_low: float    # low of the first `or_bars` bars
    or_breakout_pct: float      # (last - OR high) / OR high (>0 => broke out up)
    intraday_momentum: float    # (last - first open) / first open
    relative_volume: float      # current bar volume / average bar volume

    def summary(self) -> str:
        return (f"last {self.last:.2f} | vwap {self.vwap:.2f} "
                f"({(self.last - self.vwap) / self.vwap * 100:+.1f}% vs vwap) | "
                f"OR {self.opening_range_low:.2f}-{self.opening_range_high:.2f} "
                f"(breakout {self.or_breakout_pct * 100:+.1f}%) | "
                f"mom {self.intraday_momentum * 100:+.1f}% | "
                f"rel vol {self.relative_volume:.2f}x")

    def as_dict(self) -> dict:
        return asdict(self)


def compute_intraday_features(bars, or_bars: int = 5) -> IntradayFeatures | None:
    """`bars` is a list of intraday bar objects with .open/.high/.low/.close/.volume
    (chronological, oldest first). `or_bars` sets the opening-range window (e.g. 5
    one-minute bars = a 5-minute OR). Returns None if there aren't enough bars to
    form an opening range plus at least one bar beyond it."""
    if bars is None or len(bars) <= or_bars:
        return None

    closes = [b.close for b in bars]
    vols = [float(getattr(b, "volume", 0) or 0) for b in bars]
    last = closes[-1]

    # VWAP over the session using typical price; fall back to simple mean if no volume.
    tp = [(b.high + b.low + b.close) / 3 for b in bars]
    vol_sum = sum(vols)
    vwap = (sum(t * v for t, v in zip(tp, vols)) / vol_sum) if vol_sum > 0 else (sum(tp) / len(tp))

    opening = bars[:or_bars]
    or_high = max(b.high for b in opening)
    or_low = min(b.low for b in opening)
    or_breakout = (last - or_high) / or_high if or_high else 0.0

    first_open = bars[0].open
    momentum = (last - first_open) / first_open if first_open else 0.0

    # Relative volume: latest bar vs the average of prior bars (excludes the latest).
    prior = vols[:-1]
    avg_vol = (sum(prior) / len(prior)) if prior else 0.0
    rel_vol = (vols[-1] / avg_vol) if avg_vol > 0 else 0.0

    return IntradayFeatures(
        last=round(last, 2), vwap=round(vwap, 2),
        opening_range_high=round(or_high, 2), opening_range_low=round(or_low, 2),
        or_breakout_pct=round(or_breakout, 4), intraday_momentum=round(momentum, 4),
        relative_volume=round(rel_vol, 2),
    )

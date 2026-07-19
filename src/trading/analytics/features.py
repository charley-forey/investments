"""Technical feature computation from bars — a consistent feature definition used
by both backtests and live decisions. Pure math; no external data needed. Richer
alternative-data features (fundamentals, options flow) plug in behind the same
Features shape when a data feed is available (see M10 in the roadmap)."""

from __future__ import annotations

import math
from dataclasses import dataclass, asdict


@dataclass
class Features:
    last: float
    momentum_20: float       # 20-bar return
    sma_gap: float           # (fast SMA - slow SMA) / slow SMA
    realized_vol: float      # annualized, from recent daily returns
    atr_pct: float           # average true range as % of price
    dist_from_high: float    # % below the trailing high (0 = at highs)

    def summary(self) -> str:
        return (f"last {self.last:.2f} | 20d mom {self.momentum_20*100:+.1f}% | "
                f"sma gap {self.sma_gap*100:+.1f}% | vol {self.realized_vol*100:.0f}% | "
                f"atr {self.atr_pct*100:.1f}% | {self.dist_from_high*100:.1f}% off high")

    def as_dict(self) -> dict:
        return asdict(self)


def _sma(vals, n):
    return sum(vals[-n:]) / n if len(vals) >= n else (sum(vals) / len(vals) if vals else 0.0)


def compute_features(bars, fast: int = 10, slow: int = 30, vol_window: int = 20) -> Features | None:
    """`bars` is a list of objects with .open/.high/.low/.close. Returns None if
    there isn't enough history for the slow window."""
    if len(bars) < slow:
        return None
    closes = [b.close for b in bars]
    last = closes[-1]

    mom = (last - closes[-21]) / closes[-21] if len(closes) > 20 and closes[-21] else 0.0
    fast_sma, slow_sma = _sma(closes, fast), _sma(closes, slow)
    sma_gap = (fast_sma - slow_sma) / slow_sma if slow_sma else 0.0

    rets = [(closes[i] - closes[i - 1]) / closes[i - 1]
            for i in range(len(closes) - vol_window, len(closes)) if closes[i - 1] > 0]
    if len(rets) > 1:
        m = sum(rets) / len(rets)
        vol = math.sqrt(sum((r - m) ** 2 for r in rets) / (len(rets) - 1)) * math.sqrt(252)
    else:
        vol = 0.0

    trs = []
    for i in range(len(bars) - vol_window, len(bars)):
        if i <= 0:
            continue
        h, l, pc = bars[i].high, bars[i].low, bars[i - 1].close
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    atr = (sum(trs) / len(trs)) if trs else 0.0
    atr_pct = atr / last if last else 0.0

    trailing_high = max(closes[-slow:])
    dist_from_high = (last - trailing_high) / trailing_high if trailing_high else 0.0

    return Features(last=round(last, 2), momentum_20=round(mom, 4),
                    sma_gap=round(sma_gap, 4), realized_vol=round(vol, 4),
                    atr_pct=round(atr_pct, 4), dist_from_high=round(dist_from_high, 4))

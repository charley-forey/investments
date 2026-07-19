"""Broad-market regime read from SPY daily bars: trend (via SMA relationship) and
annualized realized volatility. Pure computation the agents weigh as context."""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass
class MarketRegime:
    trend: str            # 'up' | 'down' | 'sideways' | 'unknown'
    spy_last: float
    sma20: float
    sma50: float
    realized_vol_annual: float  # as a fraction (0.18 = 18%)
    vol_state: str        # 'calm' | 'normal' | 'elevated' | 'unknown'

    def summary(self) -> str:
        if self.trend == "unknown":
            return "market context unavailable (insufficient SPY history)"
        return (f"SPY {self.spy_last:.2f} | trend {self.trend} "
                f"(20d {self.sma20:.2f} vs 50d {self.sma50:.2f}) | "
                f"realized vol {self.realized_vol_annual*100:.0f}% ({self.vol_state})")


def _closes_from_df(df) -> list[float]:
    if df is None or len(df) == 0:
        return []
    return [float(row["close"]) for _, row in df.iterrows()]


def compute_regime(closes: list[float]) -> MarketRegime:
    if len(closes) < 50:
        return MarketRegime("unknown", 0.0, 0.0, 0.0, 0.0, "unknown")
    last = closes[-1]
    sma20 = sum(closes[-20:]) / 20
    sma50 = sum(closes[-50:]) / 50

    spread = (sma20 - sma50) / sma50 if sma50 else 0.0
    if spread > 0.01:
        trend = "up"
    elif spread < -0.01:
        trend = "down"
    else:
        trend = "sideways"

    # Annualized realized vol from daily log returns (last ~20 sessions).
    rets = [math.log(closes[i] / closes[i - 1]) for i in range(len(closes) - 20, len(closes))
            if closes[i - 1] > 0]
    if rets:
        mean = sum(rets) / len(rets)
        var = sum((r - mean) ** 2 for r in rets) / len(rets)
        vol = math.sqrt(var) * math.sqrt(252)
    else:
        vol = 0.0
    vol_state = "calm" if vol < 0.12 else "normal" if vol < 0.20 else "elevated"

    return MarketRegime(trend, last, sma20, sma50, round(vol, 4), vol_state)


def market_regime(broker, benchmark: str = "SPY") -> MarketRegime:
    try:
        df = broker.get_bars(benchmark, days=60)
    except Exception:
        return MarketRegime("unknown", 0.0, 0.0, 0.0, 0.0, "unknown")
    return compute_regime(_closes_from_df(df))

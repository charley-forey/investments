"""Broad-market regime read from SPY daily bars + VIX (Yahoo).

Trend via SMA relationship, annualized realized volatility from SPY, and a
forward-looking implied-vol regime from ^VIX level + short/long term slope.
"""

from __future__ import annotations

import json
import math
import urllib.request
from dataclasses import dataclass


@dataclass
class MarketRegime:
    trend: str            # 'up' | 'down' | 'sideways' | 'unknown'
    spy_last: float
    sma20: float
    sma50: float
    realized_vol_annual: float  # as a fraction (0.18 = 18%)
    vol_state: str        # 'calm' | 'normal' | 'elevated' | 'unknown'
    vix: float | None = None
    vix_slope: float | None = None   # short (9d) vs longer (~30d) VIX SMA gap
    iv_regime: str = "unknown"       # 'cheap' | 'normal' | 'rich' | 'unknown'

    def summary(self) -> str:
        if self.trend == "unknown":
            return "market context unavailable (insufficient SPY history)"
        line = (f"SPY {self.spy_last:.2f} | trend {self.trend} "
                f"(20d {self.sma20:.2f} vs 50d {self.sma50:.2f}) | "
                f"realized vol {self.realized_vol_annual*100:.0f}% ({self.vol_state})")
        if self.vix is not None:
            slope = ""
            if self.vix_slope is not None:
                slope = f", term slope {self.vix_slope:+.1f}"
            line += f" | VIX {self.vix:.1f}{slope} (IV {self.iv_regime})"
        return line


def _closes_from_df(df) -> list[float]:
    if df is None or len(df) == 0:
        return []
    return [float(row["close"]) for _, row in df.iterrows()]


def compute_regime(closes: list[float], *, vix_closes: list[float] | None = None) -> MarketRegime:
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

    vix = None
    vix_slope = None
    iv_regime = "unknown"
    if vix_closes and len(vix_closes) >= 5:
        vix = float(vix_closes[-1])
        short = sum(vix_closes[-min(9, len(vix_closes)):]) / min(9, len(vix_closes))
        long = sum(vix_closes[-min(30, len(vix_closes)):]) / min(30, len(vix_closes))
        vix_slope = round(short - long, 2)
        if vix < 15:
            iv_regime = "cheap"
        elif vix < 22:
            iv_regime = "normal"
        else:
            iv_regime = "rich"

    return MarketRegime(
        trend, last, sma20, sma50, round(vol, 4), vol_state,
        vix=vix, vix_slope=vix_slope, iv_regime=iv_regime,
    )


def _fetch_vix_closes(days: int = 40, timeout: float = 4.0) -> list[float]:
    """Daily ^VIX closes from Yahoo chart API. Best-effort; returns [] on failure."""
    url = (
        "https://query1.finance.yahoo.com/v8/finance/chart/%5EVIX"
        f"?range={max(days, 30)}d&interval=1d"
    )
    req = urllib.request.Request(
        url, headers={"User-Agent": "trading-bot/0.1 (vix; local paper research)"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    result = (payload.get("chart") or {}).get("result") or []
    if not result:
        return []
    closes = (((result[0].get("indicators") or {}).get("quote") or [{}])[0].get("close") or [])
    return [float(c) for c in closes if c is not None]


def market_regime(broker, benchmark: str = "SPY",
                  *, vix_fetcher=_fetch_vix_closes) -> MarketRegime:
    try:
        df = broker.get_bars(benchmark, days=60)
    except Exception:
        return MarketRegime("unknown", 0.0, 0.0, 0.0, 0.0, "unknown")
    vix_closes: list[float] = []
    try:
        vix_closes = vix_fetcher() or []
    except Exception:
        vix_closes = []
    return compute_regime(_closes_from_df(df), vix_closes=vix_closes)

"""Multi-signal OpportunityScore + movers ranking (deterministic, no LLM)."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

from .features import compute_features


@dataclass
class SignalBreakdown:
    rvol: float = 0.0
    day_pct: float = 0.0
    gap_pct: float = 0.0
    range_break: float = 0.0
    rs_vs_spy: float = 0.0
    news_spike: float = 0.0
    calendar: float = 0.0
    social: float = 0.0
    liquidity_ok: bool = True


@dataclass
class Opportunity:
    symbol: str
    score: float
    last: float
    day_pct: float
    gap_pct: float
    rvol: float
    atr_pct: float
    momentum: float
    spread_bps: float | None
    template: str | None = None
    discovery_source: str = "scanner"
    signal_breakdown: dict = field(default_factory=dict)
    trigger_direction: str = "near"
    trigger_level: float = 0.0

    def as_dict(self) -> dict:
        d = asdict(self)
        return d


# Default weights (sum roughly to scoring scale). Tunable via journal kv later.
DEFAULT_WEIGHTS = {
    "rvol": 22.0,
    "day_pct": 18.0,
    "gap_pct": 12.0,
    "range_break": 18.0,
    "rs_vs_spy": 12.0,
    "news_spike": 10.0,
    "calendar": 5.0,
    "social": 3.0,
}


def _clamp(x: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, x))


def _bars_shim(df):
    return [type("B", (), {
        "open": float(r["open"]), "high": float(r["high"]),
        "low": float(r["low"]), "close": float(r["close"]),
        "volume": float(r.get("volume", 0) or 0),
    })() for _, r in df.iterrows()]


def suggest_template(day_pct: float, rvol: float, range_break: float,
                     sma_gap: float, dist20: float, dist_from_high: float) -> str | None:
    """Map dominant signals to a strategy tag from the registry, or None.

    Every branch here must name a tag that exists in `trading.strategies` and has a
    backtest signal behind it, because this function IS the funnel: the templates it
    emits are injected into the strategy prompt as "prefer these strategy_tags"
    (orchestrator) and become the `template` column the grading ledger slices by.

    Four branches were deleted rather than fixed:

    * `news-impulse` — tested `news_spike >= 0.6` where `news_spike` is
      `_clamp(news_count_24h / 5.0)`, i.e. **3+ headlines in 24h** plus a 2% move. A
      headline *count*, no sentiment, no direction: every megacap on any active day.
      It labelled 127 of 208 graded candidates and its proposals graded -$553. There
      is no historical headline store, so it could never be backtested either.
    * `relative-strength-long/short` — was just the `day_pct >= 0` fallthrough, the
      default bucket rather than a signal, despite the name promising a comparison
      against SPY. Graded -$901 / -$169.
    * `gap-and-go` — no backtestable signal and no path to one.
    * `vwap-mean-reversion` — tested `day_pct <= -0.02 and rvol >= 1.3` and touched
      no VWAP at all.

    Returning None is deliberate and safe: a candidate still scores and still
    surfaces, it just carries no strategy suggestion. Every consumer already guards
    for it (`movers` prints "?", the grading queries filter `template IS NOT NULL`).
    """
    # Uptrend, pulled back to the moving average, still near the highs. Mirrors
    # backtest.strategies.trend_pullback_long, the one signal with validated edge.
    if sma_gap > 0 and -0.02 <= dist20 <= 0.02 and dist_from_high >= -0.10:
        return "trend-pullback-long"
    if range_break >= 0.6 and rvol >= 1.2:
        return "extended-from-sma"
    if abs(day_pct) >= 0.025 and rvol >= 1.5:
        return "momentum-continuation"
    return None


def score_opportunity(
    *,
    symbol: str,
    last: float,
    day_pct: float,
    gap_pct: float,
    rvol: float,
    atr_pct: float,
    momentum: float,
    spread_bps: float | None,
    dist20: float = 0.0,
    # Trend shape, for the trend-pullback-long branch of suggest_template. Default
    # to values that cannot match it, so a caller that omits them gets no tag rather
    # than a wrong one.
    sma_gap: float = 0.0,
    dist_from_high: float = -1.0,
    spy_day_pct: float = 0.0,
    news_count_24h: int = 0,
    days_to_event: int | None = None,
    social_mentions: int = 0,
    min_price: float = 5.0,
    min_adv: float = 0.0,
    adv: float = 0.0,
    max_spread_bps: float = 40.0,
    weights: dict[str, float] | None = None,
) -> Opportunity | None:
    """Return an Opportunity or None if liquidity filters fail."""
    w = dict(DEFAULT_WEIGHTS)
    if weights:
        w.update(weights)

    if last < min_price:
        return None
    if min_adv > 0 and adv > 0 and adv < min_adv:
        return None
    if spread_bps is not None and spread_bps > max_spread_bps:
        return None

    # Normalize component scores 0..1 then weight.
    rvol_s = _clamp((rvol - 0.8) / 3.0)  # 0.8→0, 3.8→1
    day_s = _clamp(abs(day_pct) / 0.06)
    gap_s = _clamp(abs(gap_pct) / 0.04)
    # Range break proxy: extended vs 20d SMA with volume
    range_s = _clamp(max(0.0, abs(dist20) - 0.01) / 0.05) * _clamp(rvol / 2.0)
    rs = day_pct - spy_day_pct
    rs_s = _clamp(abs(rs) / 0.04)
    news_s = _clamp(news_count_24h / 5.0)
    # Calendar: binary event soon → mild penalty for directional chase (signed negative)
    if days_to_event is None:
        cal_s = 0.0
        cal_signed = 0.0
    elif days_to_event <= 1:
        cal_s = 1.0
        cal_signed = -1.0  # caution
    elif days_to_event <= 5:
        cal_s = 0.4
        cal_signed = -0.3
    else:
        cal_s = 0.0
        cal_signed = 0.0
    social_s = _clamp(social_mentions / 20.0)

    score = (
        w["rvol"] * rvol_s
        + w["day_pct"] * day_s
        + w["gap_pct"] * gap_s
        + w["range_break"] * range_s
        + w["rs_vs_spy"] * rs_s
        + w["news_spike"] * news_s
        + w["calendar"] * cal_signed  # can reduce score near events
        + w["social"] * social_s
    )
    score = _clamp(score)

    breakdown = SignalBreakdown(
        rvol=round(rvol_s, 3), day_pct=round(day_s, 3), gap_pct=round(gap_s, 3),
        range_break=round(range_s, 3), rs_vs_spy=round(rs_s, 3),
        news_spike=round(news_s, 3), calendar=round(cal_signed, 3),
        social=round(social_s, 3), liquidity_ok=True,
    )
    template = suggest_template(day_pct, rvol, range_s,
                                sma_gap, dist20, dist_from_high)

    # Soft trigger: near last for continuation, or above/below based on direction.
    if day_pct >= 0:
        direction, level = "above", round(last * 0.998, 2)
    else:
        direction, level = "below", round(last * 1.002, 2)

    return Opportunity(
        symbol=symbol.upper(), score=round(score, 1), last=round(last, 2),
        day_pct=round(day_pct, 4), gap_pct=round(gap_pct, 4),
        rvol=round(rvol, 2), atr_pct=round(atr_pct, 4),
        momentum=round(momentum, 4),
        spread_bps=round(spread_bps, 1) if spread_bps is not None else None,
        template=template, discovery_source="scanner",
        signal_breakdown=asdict(breakdown),
        trigger_direction=direction, trigger_level=level,
    )


def scan_symbol(broker, symbol: str, *, spy_day_pct: float = 0.0,
                news_count_24h: int = 0, days_to_event: int | None = None,
                social_mentions: int = 0, min_price: float = 5.0,
                min_adv: float = 0.0, max_spread_bps: float = 40.0,
                bars_lookback: int = 40,
                weights: dict[str, float] | None = None) -> Opportunity | None:
    """Fetch bars/quote for one symbol and score it. Best-effort; None on failure."""
    try:
        df = broker.get_bars(symbol, days=max(bars_lookback, 40))
    except Exception:
        return None
    if df is None or len(df) < 30:
        return None
    bars = _bars_shim(df)
    feats = compute_features(bars)
    if feats is None:
        return None
    closes = [b.close for b in bars]
    opens = [b.open for b in bars]
    vols = [b.volume for b in bars]
    last = closes[-1]
    prev = closes[-2] if len(closes) > 1 else last
    day_pct = (last - prev) / prev if prev else 0.0
    gap_pct = (opens[-1] - prev) / prev if prev else 0.0
    avg_vol = (sum(vols[-21:-1]) / 20) if len(vols) > 21 else (sum(vols[:-1]) / max(1, len(vols) - 1))
    rvol = (vols[-1] / avg_vol) if avg_vol > 0 else 0.0
    adv = avg_vol
    sma20 = sum(closes[-20:]) / 20 if len(closes) >= 20 else last
    dist20 = (last - sma20) / sma20 if sma20 else 0.0

    spread_bps = None
    try:
        q = broker.get_quote(symbol)
        if q.mid:
            last = q.mid
            spread_bps = (q.spread / q.mid * 10000) if q.spread is not None else None
    except Exception:
        pass

    return score_opportunity(
        symbol=symbol, last=last, day_pct=day_pct, gap_pct=gap_pct, rvol=rvol,
        atr_pct=feats.atr_pct, momentum=feats.momentum_20, spread_bps=spread_bps,
        dist20=dist20, sma_gap=feats.sma_gap, dist_from_high=feats.dist_from_high,
        spy_day_pct=spy_day_pct, news_count_24h=news_count_24h,
        days_to_event=days_to_event, social_mentions=social_mentions,
        min_price=min_price, min_adv=min_adv, adv=adv,
        max_spread_bps=max_spread_bps, weights=weights,
    )


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

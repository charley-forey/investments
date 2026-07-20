"""Cross-asset risk regime read: fold SPY trend, SPY realized vol, VIX level, and
market breadth into one risk-on/risk-off label plus a numeric risk score. Pure —
every input is passed in (compute the pieces from market_context/features and hand
them here). Degrades to a 'neutral'/'unknown' read on missing inputs, never raises."""

from __future__ import annotations

from dataclasses import dataclass, asdict


@dataclass
class RegimeAssessment:
    label: str            # 'risk-on' | 'risk-off' | 'neutral' | 'unknown'
    risk_score: float     # 0..100; higher = more risk-off / defensive
    drivers: list[str]    # short reasons behind the score

    def summary(self) -> str:
        drv = ("; ".join(self.drivers)) if self.drivers else "no signals"
        return f"regime {self.label} (risk score {self.risk_score:.0f}/100) — {drv}"

    def as_dict(self) -> dict:
        return asdict(self)


def assess_regime(spy_trend: str | None = None,
                  spy_vol: float | None = None,
                  vix_level: float | None = None,
                  breadth_pct: float | None = None) -> RegimeAssessment:
    """Combine inputs into a defensive-risk score (0 calm/risk-on .. 100 stressed):
      - spy_trend: 'up'|'down'|'sideways' (from market_context.MarketRegime.trend)
      - spy_vol:   annualized realized vol as a fraction (0.18 = 18%)
      - vix_level: VIX index level (e.g. 14, 28)
      - breadth_pct: % of a universe above a trend line (e.g. 55.0)
    Any input may be None; the score is the mean of the signals that are present."""
    score = 0.0
    n = 0
    drivers: list[str] = []

    if spy_trend:
        s = {"up": 15.0, "sideways": 50.0, "down": 85.0}.get(spy_trend)
        if s is not None:
            score += s
            n += 1
            drivers.append(f"SPY trend {spy_trend}")

    if spy_vol is not None:
        # 12% -> calm, 30%+ -> stressed. Clamp to 0..100.
        s = max(0.0, min(100.0, (spy_vol - 0.12) / (0.30 - 0.12) * 100.0))
        score += s
        n += 1
        drivers.append(f"realized vol {spy_vol * 100:.0f}%")

    if vix_level is not None:
        # <15 calm, >25 risk-off. Map 12->0, 30->100.
        s = max(0.0, min(100.0, (vix_level - 12.0) / (30.0 - 12.0) * 100.0))
        score += s
        n += 1
        tag = "elevated" if vix_level > 25 else "calm" if vix_level < 15 else "normal"
        drivers.append(f"VIX {vix_level:.0f} ({tag})")

    if breadth_pct is not None:
        # High breadth = risk-on (low risk). 40% is the weak line; invert to risk.
        s = max(0.0, min(100.0, 100.0 - breadth_pct))
        score += s
        n += 1
        tag = "weak" if breadth_pct < 40 else "broad" if breadth_pct > 60 else "mixed"
        drivers.append(f"breadth {breadth_pct:.0f}% ({tag})")

    if n == 0:
        return RegimeAssessment("unknown", 0.0, [])

    risk = round(score / n, 1)
    label = "risk-off" if risk >= 60 else "risk-on" if risk <= 40 else "neutral"
    return RegimeAssessment(label=label, risk_score=risk, drivers=drivers)

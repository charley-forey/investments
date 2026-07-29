"""Execution-quality helpers: smart limit placement and realized slippage. Pure
functions the pipeline and analytics use to stop leaking edge at the point of
execution."""

from __future__ import annotations

from dataclasses import dataclass


def limit_in_spread(bid: float, ask: float, side: str, aggressiveness: float = 0.5) -> float:
    """Place a limit price inside the spread. aggressiveness 0 = passive (join your
    side), 1 = aggressive (cross to the other side); 0.5 = midpoint. Falls back to
    whichever quote is available if one side is missing."""
    if bid <= 0 or ask <= 0 or ask < bid:
        return ask or bid
    aggressiveness = min(max(aggressiveness, 0.0), 1.0)
    spread = ask - bid
    if side == "buy":
        return round(bid + spread * aggressiveness, 2)
    return round(ask - spread * aggressiveness, 2)


def realized_slippage_bps(reference: float, fill: float, side: str) -> float:
    """Implementation shortfall in basis points vs a reference (arrival) price.
    Positive = worse than reference (paid up on a buy / sold cheap on a sell)."""
    if reference <= 0:
        return 0.0
    if side == "buy":
        return (fill - reference) / reference * 10_000
    return (reference - fill) / reference * 10_000


@dataclass
class FillQuality:
    fills: int
    avg_slippage_bps: float
    worst_slippage_bps: float
    suggested_cost_hurdle_bps: float

    def summary(self) -> str:
        return (f"{self.fills} fills | avg slippage {self.avg_slippage_bps:+.1f} bps | "
                f"worst {self.worst_slippage_bps:+.1f} bps | "
                f"suggested hurdle slippage {self.suggested_cost_hurdle_bps:.1f} bps")


def fill_quality_report(journal, floor_bps: float = 1.0) -> FillQuality:
    """Aggregate realized slippage from recorded fills and suggest a cost-hurdle
    slippage assumption (so the hurdle self-calibrates from real fills rather than a
    static guess). Uses the average of positive (adverse) slippage, floored."""
    rows = journal.recorded_slippage()
    if not rows:
        return FillQuality(0, 0.0, 0.0, floor_bps)
    vals = [r for r in rows if r is not None]
    if not vals:
        return FillQuality(0, 0.0, 0.0, floor_bps)
    avg = sum(vals) / len(vals)
    worst = max(vals)
    adverse = [v for v in vals if v > 0]
    suggested = max(floor_bps, sum(adverse) / len(adverse)) if adverse else floor_bps
    return FillQuality(len(vals), round(avg, 2), round(worst, 2), round(suggested, 2))

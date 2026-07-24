"""Systematic vol-risk-premium structure selection.

Turns an IV-rank + regime + event read into a defined-risk vertical suggestion:
sell premium (credit) when IV is rich and no binary event sits inside the expiry;
buy premium (debit) when IV is cheap and a dated catalyst is coming. Pure decision
logic — analytics.options.build_vertical constructs the legs, the guardrail caps
the risk, and the agent pulls the trigger via propose_vertical. The edge is
*discovered* by the grading loop, not assumed here.
"""

from __future__ import annotations

HIGH_IV_RANK = 70.0
LOW_IV_RANK = 30.0

_TREND_DIR = {"up": "bullish", "down": "bearish"}


def suggest_vol_structure(
    iv_rank: float | None, regime_trend: str | None, has_event_in_window: bool,
    *, high: float = HIGH_IV_RANK, low: float = LOW_IV_RANK,
) -> tuple[str, str] | None:
    """Return (mode, direction), or None when no clean vol trade presents.

    - Rich IV (>= high) and NO event inside the expiry window -> SELL premium
      (credit), leaning with the trend. Selling into a known binary is the classic
      trap (IV is rich for a reason), so an event vetoes the credit.
    - Cheap IV (<= low) and a dated catalyst in the window -> BUY premium (debit),
      leaning with the trend.
    A sideways/unknown tape gives no directional lean for a single vertical -> None.
    """
    if iv_rank is None:
        return None
    direction = _TREND_DIR.get(regime_trend or "")
    if direction is None:
        return None
    if iv_rank >= high and not has_event_in_window:
        return "credit", direction
    if iv_rank <= low and has_event_in_window:
        return "debit", direction
    return None


def describe_suggestion(suggestion: tuple[str, str] | None, symbol: str) -> str:
    if suggestion is None:
        return ""
    mode, direction = suggestion
    verb = "sell premium" if mode == "credit" else "buy premium"
    return (f"Vol-premium read: {verb} — consider propose_vertical structure={mode} "
            f"direction={direction} for {symbol} (defined-risk; grading will judge it).")

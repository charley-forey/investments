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
# Minimum recent move before a sideways tape gives a directional lean. Below this
# the mean-reversion read is noise and there is no side worth picking.
MIN_STRETCH = 0.03

_TREND_DIR = {"up": "bullish", "down": "bearish"}


def suggest_vol_structure(
    iv_rank: float | None, regime_trend: str | None, has_event_in_window: bool,
    *, high: float = HIGH_IV_RANK, low: float = LOW_IV_RANK,
    stretch: float | None = None,
) -> tuple[str, str] | None:
    """Return (mode, direction), or None when no clean vol trade presents.

    - Rich IV (>= high) and NO event inside the expiry window -> SELL premium
      (credit). Selling into a known binary is the classic trap (IV is rich for a
      reason), so an event vetoes the credit.
    - Cheap IV (<= low) and a dated catalyst in the window -> BUY premium (debit).

    Direction comes from the trend when there is one. In a SIDEWAYS tape it comes
    from `stretch` (the recent 20-day move) instead, leaning AGAINST it: a name that
    has run up inside a rangebound tape is the one to sell calls against; one that
    has dumped is the one to sell puts against.

    That fallback is the whole point of this strategy. Requiring a trend meant the
    function returned None in sideways tape — the regime the system is actually in
    — so a module built to diversify away from trend-following could only fire when
    the trend-followers were already firing. Short-vol and mean-reverting is the
    uncorrelated bet; gating it on trend made it one more trend bet.
    """
    if iv_rank is None:
        return None
    direction = _TREND_DIR.get(regime_trend or "")
    if direction is None:
        if regime_trend != "sideways" or stretch is None or abs(stretch) < MIN_STRETCH:
            return None
        # Lean against the move: a credit spread wants price to stay away from the
        # strikes it sold, so sell the side price has already run toward.
        direction = "bearish" if stretch > 0 else "bullish"
    if iv_rank >= high and not has_event_in_window:
        return "credit", direction
    if iv_rank <= low and has_event_in_window:
        return "debit", direction
    return None


def scan_context(journal, regime_trend: str | None, *, limit: int = 5) -> str:
    """Names whose stored IV rank presents a vol trade right now, for the prompt.

    This module was already wired -- into `get_options_chain`, a tool the agent
    called ZERO times on 2026-07-29 across 1,049 tool calls. So the read was not
    unwired, it was undiscoverable: it only appeared once the agent had already
    decided to look at a chain, which is the decision it was supposed to inform.

    Surfacing it alongside sweep_context and regime_context puts it where the agent
    reads without being asked. Uses the latest stored snapshot per symbol, so it
    costs one indexed query and no market data call.
    """
    try:
        rows = journal.conn.execute(
            "SELECT symbol, iv_rank, features_json FROM signal_snapshot s "
            "WHERE iv_rank IS NOT NULL AND id IN "
            "  (SELECT MAX(id) FROM signal_snapshot WHERE iv_rank IS NOT NULL "
            "   GROUP BY symbol) "
            "ORDER BY ABS(iv_rank - 50) DESC LIMIT 40"
        ).fetchall()
    except Exception:
        return ""
    import json as _json

    picks = []
    for r in rows:
        stretch = None
        try:
            feats = _json.loads(r["features_json"] or "{}")
            stretch = feats.get("momentum_20")
        except Exception:
            pass
        # No event lookup here: this is a cheap standing read, and the credit
        # branch's event veto is enforced where it matters -- in the chain tool and
        # by the guardrail event wall. A name surfaced here still has to survive it.
        sug = suggest_vol_structure(r["iv_rank"], regime_trend, False, stretch=stretch)
        if sug:
            picks.append((r["symbol"], r["iv_rank"], sug))
        if len(picks) >= limit:
            break
    if not picks:
        return ""
    lines = ["Vol-premium read (short-vol / mean-reverting — the one strategy here "
             "that is NOT a trend bet; check the chain before acting, and never sell "
             "premium into a binary event):"]
    for sym, rank, (mode, direction) in picks:
        verb = "sell premium" if mode == "credit" else "buy premium"
        lines.append(f"  {sym}: IV rank {rank:.0f}% — {verb}, "
                     f"propose_vertical(structure='{mode}', direction='{direction}')")
    return "\n".join(lines)


def latest_stretch(journal, symbol: str) -> float | None:
    """Most recent 20-day move for a symbol, for the sideways-tape lean.

    Shared by the chain tool so both surfaces answer the same question the same
    way: a name shown in scan_context and then looked up would otherwise get no
    vol hint at all in sideways tape, which reads as the two disagreeing.
    """
    import json as _json

    try:
        row = journal.conn.execute(
            "SELECT features_json FROM signal_snapshot WHERE symbol=? "
            "AND features_json IS NOT NULL ORDER BY id DESC LIMIT 1",
            (symbol.upper(),)).fetchone()
        if row:
            return _json.loads(row["features_json"] or "{}").get("momentum_20")
    except Exception:
        pass
    return None


def describe_suggestion(suggestion: tuple[str, str] | None, symbol: str) -> str:
    if suggestion is None:
        return ""
    mode, direction = suggestion
    verb = "sell premium" if mode == "credit" else "buy premium"
    return (f"Vol-premium read: {verb} — consider propose_vertical structure={mode} "
            f"direction={direction} for {symbol} (defined-risk; grading will judge it).")

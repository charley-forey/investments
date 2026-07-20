"""Deterministic exit evaluation — no LLM, no network.

`evaluate_exits` scans open positions against a set of thresholds and returns
concrete close/roll actions. Every rule is pure and independently testable:
pass only the threshold you want and leave the rest unset. Bad input is
swallowed per-position (best-effort), never raised.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from ..broker.occ import parse_occ

Urgency = str  # 'high' | 'normal' | 'low'


@dataclass
class ExitAction:
    symbol: str
    action: str          # 'close' | 'roll'
    reason: str
    urgency: Urgency = "normal"


@dataclass
class ExitRules:
    """Thresholds for exit evaluation. Anything left None disables that rule.

    high_water / open_dates are passed IN (the caller owns the state — typically
    derived from journal.open_lots and per-symbol mark history). high_water is the
    peak *favorable* mark per symbol: the high for longs, the low for shorts.
    """

    stop_loss_pct: float | None = None       # e.g. 8.0 -> close at -8% adverse
    take_profit_pct: float | None = None     # e.g. 25.0 -> close at +25%
    trailing_pct: float | None = None        # e.g. 10.0 -> close 10% off peak
    max_holding_days: int | None = None
    option_roll_dte: int | None = None       # roll options with DTE < this
    high_water: dict[str, float] = field(default_factory=dict)
    open_dates: dict[str, date] = field(default_factory=dict)
    as_of: date | None = None                # defaults to today for DTE / holding


def _mark(marks, symbol):
    """Resolve a symbol's current price from a dict of float-or-Quote. None if
    absent or unusable."""
    m = marks.get(symbol) if marks else None
    if m is None:
        return None
    mid = getattr(m, "mid", None)  # Quote
    val = mid if mid is not None else m
    try:
        val = float(val)
    except (TypeError, ValueError):
        return None
    return val if val > 0 else None


def pnl_pct(pos, mark) -> float | None:
    """Signed return % for the position. Uses the current mark vs avg entry when
    available; falls back to unrealized_pl / cost basis from the position view."""
    qty = getattr(pos, "qty", 0) or 0
    direction = 1 if qty >= 0 else -1
    entry = getattr(pos, "avg_entry_price", 0) or 0
    if mark is not None and entry > 0:
        return direction * (mark - entry) / entry * 100.0
    # Fallback: cost basis = market_value - unrealized_pl.
    upl = getattr(pos, "unrealized_pl", None)
    mv = getattr(pos, "market_value", None)
    if upl is None or mv is None:
        return None
    cost = abs(mv - upl)
    if cost <= 0:
        return None
    return upl / cost * 100.0


def _stop_loss(pos, pl, rules) -> ExitAction | None:
    if rules.stop_loss_pct is None or pl is None:
        return None
    if pl <= -abs(rules.stop_loss_pct):
        return ExitAction(pos.symbol, "close",
                          f"hard stop: {pl:+.1f}% <= -{abs(rules.stop_loss_pct):.1f}%",
                          "high")
    return None


def _trailing_stop(pos, mark, rules) -> ExitAction | None:
    if rules.trailing_pct is None or mark is None:
        return None
    hwm = rules.high_water.get(getattr(pos, "symbol", ""))
    if not hwm or hwm <= 0:
        return None
    direction = 1 if (getattr(pos, "qty", 0) or 0) >= 0 else -1
    adverse = direction * (hwm - mark) / hwm * 100.0  # drawdown from peak-favorable
    if adverse >= abs(rules.trailing_pct):
        return ExitAction(pos.symbol, "close",
                          f"trailing stop: {adverse:.1f}% off peak {hwm:g}", "high")
    return None


def _take_profit(pos, pl, rules) -> ExitAction | None:
    if rules.take_profit_pct is None or pl is None:
        return None
    if pl >= abs(rules.take_profit_pct):
        return ExitAction(pos.symbol, "close",
                          f"profit target: {pl:+.1f}% >= {abs(rules.take_profit_pct):.1f}%",
                          "normal")
    return None


def _time_exit(pos, rules, as_of) -> ExitAction | None:
    if rules.max_holding_days is None:
        return None
    opened = rules.open_dates.get(getattr(pos, "symbol", ""))
    if opened is None:
        return None
    held = (as_of - opened).days
    if held >= rules.max_holding_days:
        return ExitAction(pos.symbol, "close",
                          f"time exit: held {held}d >= {rules.max_holding_days}d", "low")
    return None


def _expiry_roll(pos, rules, as_of) -> ExitAction | None:
    if rules.option_roll_dte is None:
        return None
    if getattr(pos, "asset_class", "stock") != "option":
        return None
    try:
        expiry = parse_occ(pos.symbol).expiry
    except Exception:
        return None
    dte = (expiry - as_of).days
    if dte < rules.option_roll_dte:
        urgency = "high" if dte <= 1 else "normal"
        return ExitAction(pos.symbol, "roll",
                          f"expiry roll: {dte}d to expiry < {rules.option_roll_dte}d",
                          urgency)
    return None


def evaluate_exits(positions, marks, *, rules: ExitRules) -> list[ExitAction]:
    """Evaluate every position against `rules`; at most one action per position,
    highest-priority rule wins (stop > trailing > roll > take-profit > time)."""
    as_of = rules.as_of or date.today()
    out: list[ExitAction] = []
    for pos in positions or []:
        try:
            mark = _mark(marks, getattr(pos, "symbol", None))
            pl = pnl_pct(pos, mark)
            action = (
                _stop_loss(pos, pl, rules)
                or _trailing_stop(pos, mark, rules)
                or _expiry_roll(pos, rules, as_of)
                or _take_profit(pos, pl, rules)
                or _time_exit(pos, rules, as_of)
            )
            if action is not None:
                out.append(action)
        except Exception:
            continue  # best-effort: one bad position never sinks the scan
    return out

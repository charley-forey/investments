"""Options IV term structure & skew from a parsed chain (analytics.options.ContractRow).
Per-expiry ATM IV (nearest-the-money contract per expiry), the front-vs-back IV
slope, and put/call skew per expiry. Pure and defensive: rows missing IV are
skipped, and every function degrades to empty/None rather than raising."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date


@dataclass
class TermPoint:
    expiry: date
    dte: int
    atm_iv: float


def _atm_row(rows, expiry, right=None):
    """Nearest-the-money row with an IV for one expiry (optionally one right)."""
    cand = [r for r in rows
            if r.expiry == expiry and r.iv is not None
            and (right is None or r.right == right)]
    return min(cand, key=lambda r: abs(r.moneyness)) if cand else None


def term_structure(rows) -> list[TermPoint]:
    """Per-expiry ATM IV, sorted by expiry. ATM = nearest-the-money contract with an
    IV at that expiry (either right). Empty list if no rows carry IV."""
    expiries = sorted({r.expiry for r in rows if r.iv is not None})
    out: list[TermPoint] = []
    for exp in expiries:
        atm = _atm_row(rows, exp)
        if atm is not None and atm.iv is not None:
            out.append(TermPoint(expiry=exp, dte=atm.dte, atm_iv=atm.iv))
    return out


def iv_term_slope(rows) -> float | None:
    """Back ATM IV minus front ATM IV across the term structure. >0 => upward-sloping
    (contango, calm); <0 => inverted (front-end fear). None if <2 expiries with IV."""
    ts = term_structure(rows)
    if len(ts) < 2:
        return None
    return round(ts[-1].atm_iv - ts[0].atm_iv, 4)


@dataclass
class SkewPoint:
    expiry: date
    dte: int
    put_iv: float
    call_iv: float
    skew: float           # put_iv - call_iv (>0 => downside bid)


def skew_by_expiry(rows) -> list[SkewPoint]:
    """Per-expiry ATM put/call IV skew (put IV - call IV), sorted by expiry. Only
    expiries with both an ATM put and an ATM call IV are included."""
    expiries = sorted({r.expiry for r in rows if r.iv is not None})
    out: list[SkewPoint] = []
    for exp in expiries:
        put = _atm_row(rows, exp, "put")
        call = _atm_row(rows, exp, "call")
        if put is None or call is None or put.iv is None or call.iv is None:
            continue
        out.append(SkewPoint(expiry=exp, dte=put.dte, put_iv=put.iv,
                             call_iv=call.iv, skew=round(put.iv - call.iv, 4)))
    return out

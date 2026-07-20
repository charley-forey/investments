"""Pure limit-pricing helpers for submitting marketable / mid-pegged orders
instead of naive quotes. Cents-rounded, safe on zero/crossed markets, no I/O.

Shares the in-spread placement math with execution.limit_in_spread so buy/sell
aggressiveness stays consistent across the codebase.
"""

from __future__ import annotations

from .execution import limit_in_spread


def marketable_limit(side: str, bid: float, ask: float, aggressiveness: float = 0.5) -> float:
    """Limit price for a stock order. aggressiveness 0 = passive (rest on your own
    side), 0.5 = mid, 1 = cross to the far touch. Buys pay toward ask, sells toward
    bid. Falls back to whichever quote exists on a zero/crossed market."""
    return limit_in_spread(bid, ask, side, aggressiveness)


def mid_peg(bid: float, ask: float) -> float:
    """Midpoint of the spread, cents-rounded. Guards zero/crossed markets by
    returning the best available quote."""
    if bid > 0 and ask > 0 and ask >= bid:
        return round((bid + ask) / 2, 2)
    return round(ask or bid or 0.0, 2)


def option_leg_limit(side: str, bid: float, ask: float, aggressiveness: float = 0.5) -> float:
    """Per-share limit for a single option leg. Same in-spread placement as stocks
    (option premiums round to the penny here; nickel/dime tick rules are a broker
    concern). Zero/crossed guarded via limit_in_spread."""
    return limit_in_spread(bid, ask, side, aggressiveness)


def net_debit_credit(legs_with_prices) -> float:
    """Signed net premium for a multi-leg order, cents-rounded. Each leg is a
    dict/object with `side` ('buy'|'sell'), `price` (per share), optional `qty`
    (contracts, default 1). Positive = net debit you pay, negative = net credit you
    receive. Legs with a missing/invalid price are skipped."""
    net = 0.0
    for leg in legs_with_prices or []:
        side = _get(leg, "side", "buy")
        price = _get(leg, "price", None)
        qty = _get(leg, "qty", 1) or 1
        try:
            price = float(price)
            qty = float(qty)
        except (TypeError, ValueError):
            continue
        if price <= 0:
            continue
        net += (1 if side == "buy" else -1) * price * qty
    return round(net, 2)


def _get(obj, key, default):
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)

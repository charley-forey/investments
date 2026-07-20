"""Volatility-targeted position sizing and risk throttles. Pure functions, all
guarded against divide-by-zero and bad input (return 0 / a safe multiplier).

Sits alongside guardrails.account_math.vol_target_size (which sizes in $ caps);
this one sizes by portfolio weight and target *volatility*, and adds drawdown /
IV throttles that scale an already-chosen size.
"""

from __future__ import annotations

import math


def vol_target_size(
    equity: float,
    price: float,
    symbol_annual_vol: float,
    *,
    target_annual_vol: float = 0.15,
    max_weight: float = 0.10,
    min_qty: int = 1,
) -> int:
    """Integer share count whose portfolio weight targets `target_annual_vol` of
    equity, i.e. weight = target_vol / symbol_vol, capped at `max_weight`.
    Higher-vol names get smaller positions. Returns 0 on bad/degenerate input."""
    if equity <= 0 or price <= 0 or symbol_annual_vol <= 0 or target_annual_vol <= 0:
        return 0
    weight = min(target_annual_vol / symbol_annual_vol, max_weight)
    cap_qty = math.floor(max_weight * equity / price)   # never exceed the weight cap
    if cap_qty <= 0:
        return 0
    qty = math.floor(weight * equity / price)
    return min(max(qty, max(min_qty, 0)), cap_qty)


def drawdown_throttle(drawdown_pct: float, *, soft: float = 0.05, hard: float = 0.15) -> float:
    """Graded 0..1 size multiplier from current drawdown (as a fraction, 0.05 = 5%):
    1.0 at/below `soft`, 0.0 at/above `hard`, linear between. Clamped to [0,1]."""
    dd = drawdown_pct if drawdown_pct > 0 else 0.0
    if hard <= soft:                       # degenerate band: step at the threshold
        return 1.0 if dd < hard else 0.0
    if dd <= soft:
        return 1.0
    if dd >= hard:
        return 0.0
    return max(0.0, min(1.0, 1.0 - (dd - soft) / (hard - soft)))


def iv_adjusted_size(base_qty: int, iv_rank: float | None, *, high: float = 80.0,
                     floor: float = 0.5) -> int:
    """Shrink an already-sized quantity when IV rank (0..100) is rich. Linear from
    full size at `high` down to `floor` x size at 100. No-op when iv_rank is None
    or below `high`."""
    if iv_rank is None or base_qty <= 0 or iv_rank <= high or high >= 100:
        return max(0, int(base_qty))
    frac = 1.0 - (1.0 - floor) * (min(iv_rank, 100.0) - high) / (100.0 - high)
    return max(0, int(math.floor(base_qty * frac)))

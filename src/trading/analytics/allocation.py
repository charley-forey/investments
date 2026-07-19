"""Capital allocation across strategies and P&L attribution.

Allocation is by realized, risk-adjusted, after-tax expectancy with a
sample-size confidence haircut — capital follows what the numbers prove, not what
the agents feel. Pure computation over the scores table."""

from __future__ import annotations

import math
from dataclasses import dataclass

from ..config import TaxRates
from ..data.journal import Journal
from .stats import StrategyStats, stats_by_tag


def _confidence(trades: int, full_at: int = 30) -> float:
    """0..1 sample-size confidence — a strategy with few trades gets a haircut so a
    lucky 3-trade streak can't hog capital."""
    return min(1.0, math.sqrt(trades / full_at)) if trades > 0 else 0.0


@dataclass
class Allocation:
    tag: str
    weight: float                 # fraction of the risk budget (0..1)
    after_tax_expectancy: float
    trades: int
    confidence: float


def allocate_capital(journal: Journal, rates: TaxRates) -> list[Allocation]:
    """Weight strategies by (positive after-tax expectancy) x confidence, normalized.
    Strategies with non-positive expectancy get zero — losers are starved, not fed."""
    by_tag = stats_by_tag(journal, rates)
    raw: dict[str, tuple[float, StrategyStats, float]] = {}
    for tag, s in by_tag.items():
        conf = _confidence(s.trades)
        score = max(0.0, s.after_tax_expectancy) * conf
        raw[tag] = (score, s, conf)

    total = sum(v[0] for v in raw.values())
    out: list[Allocation] = []
    for tag, (score, s, conf) in raw.items():
        weight = (score / total) if total > 0 else 0.0
        out.append(Allocation(tag=tag, weight=round(weight, 4),
                              after_tax_expectancy=round(s.after_tax_expectancy, 2),
                              trades=s.trades, confidence=round(conf, 3)))
    out.sort(key=lambda a: a.weight, reverse=True)
    return out


def persist_allocations(journal: Journal, allocations: list[Allocation]) -> None:
    """Store the current target weight per tag (kv_state) for the sizing layer to read."""
    for a in allocations:
        journal.set_state(f"alloc:{a.tag}", str(a.weight))


def allocation_weight(journal: Journal, tag: str) -> float:
    return float(journal.get_state(f"alloc:{tag}", "0") or 0)


@dataclass
class AttributionRow:
    tag: str
    gross_pnl: float
    after_tax_pnl: float
    share: float                  # fraction of total after-tax P&L
    trades: int


def attribution_report(journal: Journal, rates: TaxRates) -> list[AttributionRow]:
    """Decompose realized P&L by strategy — where the money actually came from."""
    by_tag = stats_by_tag(journal, rates)
    total_at = sum(s.after_tax_pnl for s in by_tag.values())
    rows = []
    for tag, s in by_tag.items():
        share = (s.after_tax_pnl / total_at) if total_at else 0.0
        rows.append(AttributionRow(tag=tag, gross_pnl=round(s.gross_pnl, 2),
                                   after_tax_pnl=round(s.after_tax_pnl, 2),
                                   share=round(share, 4), trades=s.trades))
    rows.sort(key=lambda r: r.after_tax_pnl, reverse=True)
    return rows

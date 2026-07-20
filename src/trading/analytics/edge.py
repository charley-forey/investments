"""Proof of alpha — the honest 'does it actually make money?' gate.

Reads the real (paper) scored trades and asks, per strategy, whether the realized
expectancy is positive with statistical confidence, not luck. Also compares the
account's realized return to buying-and-holding SPY. This is the evidence M19
requires before any live sizing. Pure computation over the journal + bar store.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from ..config import TaxRates
from ..data.journal import Journal

MIN_TRADES = 20  # below this a strategy is 'unproven' regardless of the point estimate


@dataclass
class StrategyEdge:
    tag: str
    trades: int
    expectancy: float       # mean per-trade P&L
    t_stat: float           # mean / (std / sqrt(n))
    significant: bool       # |t| > 2  (~95%)
    verdict: str            # 'proven' | 'negative' | 'unproven'

    def summary(self) -> str:
        return (f"{self.tag}: {self.trades} trades, expectancy ${self.expectancy:+.2f}, "
                f"t={self.t_stat:+.2f} -> {self.verdict.upper()}")


def _pnls_for_tag(journal: Journal, tag: str) -> list[float]:
    return [float(s["pnl_usd"] or 0) for s in journal.scores_for_tag(tag)]


def _t_stat(pnls: list[float]) -> float:
    n = len(pnls)
    if n < 2:
        return 0.0
    mean = sum(pnls) / n
    var = sum((x - mean) ** 2 for x in pnls) / (n - 1)
    sd = math.sqrt(var)
    if sd == 0:
        return math.inf if mean > 0 else (-math.inf if mean < 0 else 0.0)
    return mean / (sd / math.sqrt(n))


def strategy_edge(journal: Journal, tag: str) -> StrategyEdge:
    pnls = _pnls_for_tag(journal, tag)
    n = len(pnls)
    expectancy = sum(pnls) / n if n else 0.0
    t = _t_stat(pnls)
    significant = abs(t) > 2.0 and n >= MIN_TRADES
    if significant and expectancy > 0:
        verdict = "proven"
    elif significant and expectancy < 0:
        verdict = "negative"
    else:
        verdict = "unproven"
    return StrategyEdge(tag=tag, trades=n, expectancy=round(expectancy, 2),
                        t_stat=round(t, 2) if math.isfinite(t) else t,
                        significant=significant, verdict=verdict)


def strategy_edges(journal: Journal) -> list[StrategyEdge]:
    edges = [strategy_edge(journal, t) for t in journal.distinct_strategy_tags()]
    edges.sort(key=lambda e: e.expectancy, reverse=True)
    return edges


def portfolio_edge(journal: Journal) -> dict:
    """Is there any demonstrated, statistically-significant edge yet? This is the
    gate to live sizing (M19 exit)."""
    edges = strategy_edges(journal)
    proven = [e for e in edges if e.verdict == "proven"]
    return {
        "has_proven_edge": bool(proven),
        "proven_strategies": [e.tag for e in proven],
        "total_scored_trades": sum(e.trades for e in edges),
        "verdict": ("PROVEN EDGE — a strategy clears the confidence bar" if proven
                    else "NOT YET PROVEN — insufficient evidence for live sizing"),
    }


def benchmark_comparison(journal: Journal, bars_db_path: str,
                         benchmark: str = "SPY") -> dict:
    """Account realized return vs buy-and-hold SPY over the tracked period. Simple,
    honest total-return comparison from the recorded equity curve and stored bars
    (no aligned-series beta until there's enough history — YAGNI)."""
    import os

    eq = journal.equity_history(limit=1000)
    if len(eq) < 2:
        return {"available": False, "reason": "not enough equity history yet"}
    acct_return = (eq[-1]["equity"] / eq[0]["equity"] - 1.0) if eq[0]["equity"] else 0.0

    bench_return = None
    if os.path.exists(bars_db_path):
        from ..data.bars import BarStore
        store = BarStore(bars_db_path)
        try:
            bars = store.load_bars(benchmark, start=eq[0]["ts"][:10], end=eq[-1]["ts"][:10])
            if len(bars) >= 2 and bars[0].close:
                bench_return = round(bars[-1].close / bars[0].close - 1.0, 4)
        finally:
            store.close()
    out = {"available": True, "account_return": round(acct_return, 4),
           "benchmark": benchmark, "benchmark_return": bench_return}
    if bench_return is not None:
        out["excess_return"] = round(acct_return - bench_return, 4)
    return out

"""Monte Carlo bootstrap over backtest trades — turns a single backtest number into
a distribution, so 'positive expectancy' comes with a confidence interval and a
probability of profit instead of one lucky path. Pure stdlib."""

from __future__ import annotations

import random
from dataclasses import dataclass


@dataclass
class MonteCarlo:
    iterations: int
    expectancy_p5: float
    expectancy_p50: float
    expectancy_p95: float
    prob_profitable: float      # fraction of resampled paths with positive total
    worst_drawdown_p95: float   # 95th-percentile max drawdown across paths

    def summary(self) -> str:
        return (f"MC({self.iterations}): expectancy p5/p50/p95 "
                f"${self.expectancy_p5:+.2f}/${self.expectancy_p50:+.2f}/"
                f"${self.expectancy_p95:+.2f} | P(profit) {self.prob_profitable*100:.0f}% | "
                f"worst DD (p95) ${self.worst_drawdown_p95:,.2f}")


def _percentile(sorted_vals: list[float], p: float) -> float:
    if not sorted_vals:
        return 0.0
    i = min(len(sorted_vals) - 1, max(0, int(p * len(sorted_vals))))
    return sorted_vals[i]


def _max_drawdown(pnls: list[float]) -> float:
    cum = peak = mdd = 0.0
    for x in pnls:
        cum += x
        peak = max(peak, cum)
        mdd = max(mdd, peak - cum)
    return mdd


def bootstrap(trade_pnls: list[float], *, iterations: int = 2000,
              seed: int | None = None) -> MonteCarlo:
    """Resample the trade P&Ls with replacement `iterations` times; each path has the
    same length as the original. Returns the distribution of per-trade expectancy and
    the tail of drawdown."""
    n = len(trade_pnls)
    if n == 0:
        return MonteCarlo(iterations, 0, 0, 0, 0.0, 0.0)
    rng = random.Random(seed)
    exps, dds, wins = [], [], 0
    for _ in range(iterations):
        path = [trade_pnls[rng.randrange(n)] for _ in range(n)]
        total = sum(path)
        exps.append(total / n)
        dds.append(_max_drawdown(path))
        if total > 0:
            wins += 1
    exps.sort()
    dds.sort()
    return MonteCarlo(
        iterations=iterations,
        expectancy_p5=round(_percentile(exps, 0.05), 2),
        expectancy_p50=round(_percentile(exps, 0.50), 2),
        expectancy_p95=round(_percentile(exps, 0.95), 2),
        prob_profitable=round(wins / iterations, 3),
        worst_drawdown_p95=round(_percentile(dds, 0.95), 2),
    )

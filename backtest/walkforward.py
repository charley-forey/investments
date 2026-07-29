"""Walk-forward (out-of-sample) validation and the auto-gate that promotes or
demotes a strategy based on rigorous evidence rather than a single in-sample fit.
A strategy earns `candidate -> paper` only by passing walk-forward."""

from __future__ import annotations

from dataclasses import dataclass, field

from .engine import Bar, Signal, run_backtest


@dataclass
class FoldResult:
    trades: int
    expectancy: float
    net_pnl: float
    expectancy_r: float | None = None   # per-trade R, when the run used stops


@dataclass
class WalkForwardResult:
    folds: list[FoldResult] = field(default_factory=list)

    @property
    def n_folds(self) -> int:
        return len(self.folds)

    @property
    def mean_expectancy(self) -> float:
        vals = [f.expectancy for f in self.folds if f.trades > 0]
        return sum(vals) / len(vals) if vals else 0.0

    @property
    def positive_folds(self) -> int:
        return sum(1 for f in self.folds if f.trades > 0 and f.expectancy > 0)

    @property
    def evaluated_folds(self) -> int:
        return sum(1 for f in self.folds if f.trades > 0)

    def passed(self, *, min_mean_expectancy: float = 0.0,
               min_positive_fraction: float = 0.6) -> bool:
        """Pass when out-of-sample expectancy is positive on average AND positive in
        a majority of folds that actually traded — robustness, not a lucky fit.

        Gates on R, not dollars. Dollar expectancy is per-share P&L at qty=1, so
        across a universe priced $20 to $900 it ranks by share price rather than by
        edge: a mediocre signal on expensive stocks beats a good one on cheap stocks
        every time. R normalises by the risk actually taken, which is also the unit
        `min_reward_risk` is set in. Falls back to dollars only when no fold produced
        an R (no stop configured), where the two are at least monotonic per symbol."""
        if self.evaluated_folds == 0:
            return False
        frac = self.positive_folds / self.evaluated_folds
        metric = self.mean_expectancy_r
        if metric is None:
            metric = self.mean_expectancy
        return metric > min_mean_expectancy and frac >= min_positive_fraction

    @property
    def mean_expectancy_r(self) -> float | None:
        """Mean out-of-sample R per trade — the unit min_reward_risk is set in, so
        the validation speaks the same language as the decision it informs."""
        vals = [f.expectancy_r for f in self.folds
                if f.trades > 0 and f.expectancy_r is not None]
        return sum(vals) / len(vals) if vals else None

    def summary(self) -> str:
        out = (f"{self.n_folds} folds ({self.evaluated_folds} traded), "
               f"mean OOS expectancy ${self.mean_expectancy:+.2f}, "
               f"{self.positive_folds}/{self.evaluated_folds} positive")
        r = self.mean_expectancy_r
        if r is not None:
            out += f", {r:+.3f}R/trade"
        return out


def walk_forward(bars: list[Bar], signal: Signal, *, n_folds: int = 4,
                 **backtest_kwargs) -> WalkForwardResult:
    """Split the history into `n_folds` sequential out-of-sample folds and backtest
    each independently. (With parameter-fitted strategies, fit on the preceding data
    and test on each fold; the reference signals are parameter-free, so each fold is
    a clean OOS test.)"""
    result = WalkForwardResult()
    if len(bars) < n_folds * 2:
        return result
    size = len(bars) // n_folds
    for k in range(n_folds):
        start = k * size
        end = len(bars) if k == n_folds - 1 else (k + 1) * size
        fold_bars = bars[start:end]
        bt = run_backtest(fold_bars, signal, **backtest_kwargs)
        result.folds.append(FoldResult(trades=bt.n, expectancy=bt.expectancy,
                                       net_pnl=bt.net_pnl,
                                       expectancy_r=bt.expectancy_r))
    return result


def gate_strategy(journal, tag: str, wf: WalkForwardResult) -> str:
    """Auto-gate: a candidate/backtest strategy that passes walk-forward is promoted
    to paper; one that fails is held. Returns a short status string."""
    from trading.analytics import lifecycle

    stage = lifecycle.get_stage(journal, tag)
    if stage not in lifecycle.PROMOTABLE_BY_BACKTEST:
        return f"{tag}: stage '{stage}' — walk-forward gate not applicable"
    if wf.passed():
        metric = wf.mean_expectancy_r
        if metric is None:
            metric = wf.mean_expectancy
        change = lifecycle.promote_after_backtest(journal, tag, metric)
        if change:
            return f"{tag}: PASSED walk-forward -> promoted to {change.new_stage}"
    return f"{tag}: did not pass walk-forward ({wf.summary()}) — stays {stage}"

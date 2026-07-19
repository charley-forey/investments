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
        a majority of folds that actually traded — robustness, not a lucky fit."""
        if self.evaluated_folds == 0:
            return False
        frac = self.positive_folds / self.evaluated_folds
        return (self.mean_expectancy > min_mean_expectancy
                and frac >= min_positive_fraction)

    def summary(self) -> str:
        return (f"{self.n_folds} folds ({self.evaluated_folds} traded), "
                f"mean OOS expectancy ${self.mean_expectancy:+.2f}, "
                f"{self.positive_folds}/{self.evaluated_folds} positive")


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
                                       net_pnl=bt.net_pnl))
    return result


def gate_strategy(journal, tag: str, wf: WalkForwardResult) -> str:
    """Auto-gate: a candidate/backtest strategy that passes walk-forward is promoted
    to paper; one that fails is held. Returns a short status string."""
    from trading.analytics import lifecycle

    stage = lifecycle.get_stage(journal, tag)
    if stage not in ("candidate", "backtest"):
        return f"{tag}: stage '{stage}' — walk-forward gate not applicable"
    if wf.passed():
        change = lifecycle.promote_after_backtest(journal, tag, wf.mean_expectancy)
        if change:
            return f"{tag}: PASSED walk-forward -> promoted to {change.new_stage}"
    return f"{tag}: did not pass walk-forward ({wf.summary()}) — stays {stage}"

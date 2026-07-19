"""Risk/performance metrics and benchmark-relative statistics. Pure math over an
equity curve (and an aligned benchmark curve), so backtests can be judged like real
track records: Sharpe, Sortino, drawdown, and alpha/beta vs a benchmark."""

from __future__ import annotations

import math
from dataclasses import dataclass

TRADING_DAYS = 252


def returns_from_curve(curve: list[float]) -> list[float]:
    out = []
    for i in range(1, len(curve)):
        prev = curve[i - 1]
        out.append((curve[i] - prev) / prev if prev else 0.0)
    return out


def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def _std(xs: list[float]) -> float:
    if len(xs) < 2:
        return 0.0
    m = _mean(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))


def sharpe(returns: list[float], periods: int = TRADING_DAYS) -> float:
    sd = _std(returns)
    if sd == 0:
        return 0.0
    return _mean(returns) / sd * math.sqrt(periods)


def sortino(returns: list[float], periods: int = TRADING_DAYS) -> float:
    downside = [r for r in returns if r < 0]
    dd = _std(downside) if len(downside) > 1 else 0.0
    if dd == 0:
        return 0.0
    return _mean(returns) / dd * math.sqrt(periods)


def max_drawdown(curve: list[float]) -> float:
    """Largest peak-to-trough decline as a positive fraction (0.2 = 20%)."""
    peak = curve[0] if curve else 0.0
    mdd = 0.0
    for v in curve:
        peak = max(peak, v)
        if peak > 0:
            mdd = max(mdd, (peak - v) / peak)
    return mdd


def volatility(returns: list[float], periods: int = TRADING_DAYS) -> float:
    return _std(returns) * math.sqrt(periods)


def total_return(curve: list[float]) -> float:
    if len(curve) < 2 or curve[0] == 0:
        return 0.0
    return curve[-1] / curve[0] - 1.0


def alpha_beta(strategy: list[float], benchmark: list[float],
               periods: int = TRADING_DAYS) -> tuple[float, float]:
    """OLS of strategy daily returns on benchmark daily returns: beta is the slope,
    alpha is the intercept annualized. Returns (alpha_annual, beta)."""
    n = min(len(strategy), len(benchmark))
    if n < 2:
        return 0.0, 0.0
    s, b = strategy[-n:], benchmark[-n:]
    mb, ms = _mean(b), _mean(s)
    cov = sum((b[i] - mb) * (s[i] - ms) for i in range(n)) / (n - 1)
    var = sum((b[i] - mb) ** 2 for i in range(n)) / (n - 1)
    beta = cov / var if var else 0.0
    alpha_daily = ms - beta * mb
    return alpha_daily * periods, beta


@dataclass
class PerformanceMetrics:
    total_return: float
    sharpe: float
    sortino: float
    max_drawdown: float
    volatility: float
    alpha: float
    beta: float

    def summary(self) -> str:
        return (f"return {self.total_return*100:+.1f}% | Sharpe {self.sharpe:.2f} | "
                f"Sortino {self.sortino:.2f} | maxDD {self.max_drawdown*100:.1f}% | "
                f"vol {self.volatility*100:.1f}% | alpha {self.alpha*100:+.1f}% | "
                f"beta {self.beta:.2f}")


def compute_metrics(equity_curve: list[float],
                    benchmark_curve: list[float] | None = None) -> PerformanceMetrics:
    rets = returns_from_curve(equity_curve)
    a = b = 0.0
    if benchmark_curve:
        a, b = alpha_beta(rets, returns_from_curve(benchmark_curve))
    return PerformanceMetrics(
        total_return=total_return(equity_curve),
        sharpe=sharpe(rets),
        sortino=sortino(rets),
        max_drawdown=max_drawdown(equity_curve),
        volatility=volatility(rets),
        alpha=a, beta=b,
    )

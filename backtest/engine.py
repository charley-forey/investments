"""Lightweight, dependency-free bar-replay backtester.

Deliberately simple and pure so it's fast and unit-testable. Uses the same
friction model as the live guardrails (guardrails.account_math.friction_cost) so
a backtest never flatters a strategy relative to production. Feeds the
`candidate -> backtest` lifecycle gate: a strategy must clear a backtest
expectancy bar before it is allowed to paper trade.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

from trading.guardrails.account_math import friction_cost


@dataclass
class Bar:
    date: str
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass
class Trade:
    entry_idx: int
    exit_idx: int
    entry_price: float
    exit_price: float
    qty: float
    gross_pnl: float
    cost: float

    @property
    def net_pnl(self) -> float:
        return self.gross_pnl - self.cost


@dataclass
class BacktestResult:
    trades: list[Trade]
    equity_curve: list[float] = None  # per-bar mark-to-market equity

    def __post_init__(self):
        if self.equity_curve is None:
            self.equity_curve = []

    @property
    def n(self) -> int:
        return len(self.trades)

    @property
    def gross_pnl(self) -> float:
        return sum(t.gross_pnl for t in self.trades)

    @property
    def net_pnl(self) -> float:
        return sum(t.net_pnl for t in self.trades)

    @property
    def wins(self) -> int:
        return sum(1 for t in self.trades if t.net_pnl > 0)

    @property
    def win_rate(self) -> float:
        return self.wins / self.n if self.n else 0.0

    @property
    def expectancy(self) -> float:
        """Net-of-cost average P&L per trade — the gate metric."""
        return self.net_pnl / self.n if self.n else 0.0

    def summary(self) -> str:
        return (f"{self.n} trades, win {self.win_rate*100:.0f}%, "
                f"net expectancy ${self.expectancy:+.2f}, net ${self.net_pnl:+.2f}")


# A signal returns +1 (go long from this bar's close) or 0 (flat/exit) for index i.
Signal = Callable[[list[Bar], int], int]


def run_backtest(
    bars: list[Bar],
    signal: Signal,
    *,
    qty: float = 1.0,
    spread_frac: float = 0.0005,   # assumed round-trip spread as fraction of price
    slippage_bps: float = 5.0,
    starting_capital: float = 10_000.0,
) -> BacktestResult:
    """Long-flat backtest: enter long on a 0->1 signal transition (fill at close),
    exit on 1->0 (fill at close). Costs applied per round trip via friction_cost.
    Also produces a per-bar mark-to-market equity curve for risk metrics."""
    trades: list[Trade] = []
    curve: list[float] = []
    realized = 0.0
    in_pos = False
    entry_idx = 0
    entry_price = 0.0
    prev = 0

    def _cost(a: float, b: float) -> float:
        notional = (a + b) / 2 * qty
        spread_usd = spread_frac * (a + b) / 2 * qty
        return friction_cost(notional, spread_usd, slippage_bps)

    for i in range(len(bars)):
        sig = signal(bars, i)
        if not in_pos and prev == 0 and sig == 1:
            in_pos = True
            entry_idx = i
            entry_price = bars[i].close
        elif in_pos and sig == 0:
            exit_price = bars[i].close
            gross = (exit_price - entry_price) * qty
            cost = _cost(entry_price, exit_price)
            trades.append(Trade(entry_idx, i, entry_price, exit_price, qty, gross, cost))
            realized += gross - cost
            in_pos = False
        prev = sig
        unrealized = (bars[i].close - entry_price) * qty if in_pos else 0.0
        curve.append(starting_capital + realized + unrealized)

    # Close any open position at the last bar.
    if in_pos and bars:
        i = len(bars) - 1
        exit_price = bars[i].close
        gross = (exit_price - entry_price) * qty
        cost = _cost(entry_price, exit_price)
        trades.append(Trade(entry_idx, i, entry_price, exit_price, qty, gross, cost))
        realized += gross - cost
        curve[-1] = starting_capital + realized

    return BacktestResult(trades=trades, equity_curve=curve)


def bars_from_alpaca_df(df) -> list[Bar]:
    """Convert an alpaca-py bars DataFrame (MultiIndex symbol/timestamp) to Bars."""
    out: list[Bar] = []
    for idx, row in df.iterrows():
        ts = idx[1] if isinstance(idx, tuple) else idx
        out.append(Bar(
            date=str(ts)[:10], open=float(row["open"]), high=float(row["high"]),
            low=float(row["low"]), close=float(row["close"]),
            volume=float(row["volume"]),
        ))
    return out

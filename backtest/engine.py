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
    direction: int = 1              # +1 long, -1 short
    stop_price: float | None = None
    target_price: float | None = None
    exit_reason: str = "signal"     # signal | stop | target | eod

    @property
    def net_pnl(self) -> float:
        return self.gross_pnl - self.cost

    @property
    def r_multiple(self) -> float | None:
        """Net P&L in units of the risk taken. This is the number the live
        min_reward_risk floor is about, so a backtest that cannot report it cannot
        tell you whether the floor is set right."""
        if self.stop_price is None:
            return None
        risk_per_share = abs(self.entry_price - self.stop_price)
        if risk_per_share <= 0 or self.qty <= 0:
            return None
        return self.net_pnl / (risk_per_share * self.qty)


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
        base = (f"{self.n} trades, win {self.win_rate*100:.0f}%, "
                f"net expectancy ${self.expectancy:+.2f}, net ${self.net_pnl:+.2f}")
        r = self.expectancy_r
        if r is not None:
            base += f", {r:+.2f}R/trade"
        return base

    @property
    def expectancy_r(self) -> float | None:
        """Average R per trade — comparable across symbols and position sizes,
        unlike dollar expectancy."""
        rs = [t.r_multiple for t in self.trades if t.r_multiple is not None]
        return sum(rs) / len(rs) if rs else None

    def exit_breakdown(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for t in self.trades:
            out[t.exit_reason] = out.get(t.exit_reason, 0) + 1
        return out


# A signal returns +1 (long), -1 (short) or 0 (flat/exit) for index i. Shorts are
# only taken when allow_shorts=True, so existing long-flat signals are unaffected.
Signal = Callable[[list[Bar], int], int]


def run_backtest(
    bars: list[Bar],
    signal: Signal,
    *,
    qty: float = 1.0,
    spread_frac: float = 0.0005,   # assumed round-trip spread as fraction of price
    slippage_bps: float = 5.0,
    starting_capital: float = 10_000.0,
    stop_pct: float | None = None,
    target_r: float | None = None,
    trail_pct: float | None = None,
    allow_shorts: bool = False,
) -> BacktestResult:
    """Bar-replay backtest. Enter on a 0->±1 signal transition (fill at close),
    exit on a return to 0, on a reversal, or on a bracket hit.

    `stop_pct` attaches a protective stop that far from entry; `target_r` puts the
    take-profit that many multiples of the stop distance away. Both are checked
    intrabar against the bar's high/low, stop first — a bar that spans both is
    scored as a loss, because tick order is unknowable and the optimistic reading
    is how backtests lie. Without `stop_pct` the behaviour is the original
    long-flat close-to-close replay.

    `trail_pct` gives back that fraction of the peak favorable price since entry.
    Set `target_r=None` with it to test letting winners run. The trail is measured
    against the peak as of the PREVIOUS bar, never this bar's own high — using a
    high that may have printed after the exit is the same lie as the optimistic
    stop/target ordering above."""
    trades: list[Trade] = []
    curve: list[float] = []
    realized = 0.0
    direction = 0                  # 0 flat, +1 long, -1 short
    entry_idx = 0
    entry_price = 0.0
    stop_price: float | None = None
    target_price: float | None = None
    peak: float | None = None      # best favorable price since entry (trailing stop)
    prev = 0

    def _cost(a: float, b: float) -> float:
        notional = (a + b) / 2 * qty
        spread_usd = spread_frac * (a + b) / 2 * qty
        return friction_cost(notional, spread_usd, slippage_bps)

    def _close(i: int, exit_price: float, reason: str) -> float:
        gross = (exit_price - entry_price) * qty * direction
        cost = _cost(entry_price, exit_price)
        trades.append(Trade(entry_idx, i, entry_price, exit_price, qty, gross, cost,
                            direction=direction, stop_price=stop_price,
                            target_price=target_price, exit_reason=reason))
        return gross - cost

    def _brackets(px: float, d: int):
        if stop_pct is None or stop_pct <= 0:
            return None, None
        stop = px * (1 - stop_pct) if d > 0 else px * (1 + stop_pct)
        tgt = None
        if target_r and target_r > 0:
            tgt = px + d * target_r * abs(px - stop)
        return stop, tgt

    for i in range(len(bars)):
        bar = bars[i]

        # Bracket exits are checked before this bar's signal: a stop that was hit
        # intrabar happened before any close-based decision could be acted on.
        if direction != 0 and stop_price is not None:
            hit_stop = bar.low <= stop_price if direction > 0 else bar.high >= stop_price
            hit_tgt = (target_price is not None
                       and (bar.high >= target_price if direction > 0
                            else bar.low <= target_price))
            trail_price = None
            if trail_pct and trail_pct > 0 and peak is not None:
                trail_price = (peak * (1 - trail_pct) if direction > 0
                               else peak * (1 + trail_pct))
            hit_trail = trail_price is not None and (
                bar.low <= trail_price if direction > 0 else bar.high >= trail_price)
            if hit_stop:
                realized += _close(i, stop_price, "stop")
                direction = 0
            elif hit_trail:
                realized += _close(i, trail_price, "trail")
                direction = 0
            elif hit_tgt:
                realized += _close(i, target_price, "target")
                direction = 0

        # Peak updates only after this bar's exits are resolved, so a trail can
        # never be measured against a high the position never got to see.
        if direction != 0:
            peak = (max(peak, bar.high) if peak is not None else bar.high) \
                if direction > 0 else \
                (min(peak, bar.low) if peak is not None else bar.low)

        sig = signal(bars, i)
        if not allow_shorts and sig < 0:
            sig = 0
        if direction == 0 and prev == 0 and sig != 0:
            direction = 1 if sig > 0 else -1
            entry_idx = i
            entry_price = bar.close
            peak = bar.close
            stop_price, target_price = _brackets(entry_price, direction)
        elif direction != 0 and sig != direction:
            # Back to flat, or an outright reversal.
            realized += _close(i, bar.close, "signal")
            direction = 0
            if sig != 0:
                direction = 1 if sig > 0 else -1
                entry_idx = i
                entry_price = bar.close
                peak = bar.close
                stop_price, target_price = _brackets(entry_price, direction)

        prev = sig
        unrealized = ((bar.close - entry_price) * qty * direction) if direction else 0.0
        curve.append(starting_capital + realized + unrealized)

    # Close any open position at the last bar.
    if direction != 0 and bars:
        realized += _close(len(bars) - 1, bars[-1].close, "eod")
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

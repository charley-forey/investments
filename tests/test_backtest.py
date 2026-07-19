from backtest.engine import Bar, run_backtest
from backtest.strategies import sma_crossover


def make_bars(closes: list[float]) -> list[Bar]:
    return [Bar(date=f"2026-01-{i+1:02d}", open=c, high=c, low=c, close=c, volume=1e6)
            for i, c in enumerate(closes)]


def test_uptrend_sma_is_profitable_net_of_costs():
    # steady uptrend -> fast SMA above slow -> one long trade held to the end
    closes = [100 + i for i in range(60)]
    result = run_backtest(make_bars(closes), sma_crossover(fast=5, slow=20), qty=10)
    assert result.n >= 1
    assert result.net_pnl > 0
    assert result.expectancy > 0


def test_flat_market_no_or_breakeven_trades():
    closes = [100.0] * 60
    result = run_backtest(make_bars(closes), sma_crossover(fast=5, slow=20), qty=10)
    # no crossover in a flat series -> no trades, so no false profits
    assert result.n == 0
    assert result.net_pnl == 0.0


def test_costs_reduce_net_below_gross():
    closes = [100 + i for i in range(60)]
    result = run_backtest(make_bars(closes), sma_crossover(fast=5, slow=20), qty=10)
    assert result.net_pnl < result.gross_pnl  # friction always costs something


def test_summary_formats():
    closes = [100 + i for i in range(60)]
    result = run_backtest(make_bars(closes), sma_crossover(), qty=1)
    assert "trades" in result.summary()

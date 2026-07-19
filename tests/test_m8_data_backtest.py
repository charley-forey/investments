"""M8: bar store, benchmark metrics, walk-forward validation, auto-gate."""

import pytest

from backtest import metrics
from backtest.engine import Bar as BTBar
from backtest.engine import run_backtest
from backtest.strategies import sma_crossover
from backtest.walkforward import gate_strategy, walk_forward

from trading.analytics import lifecycle
from trading.data.bars import Bar, BarStore
from trading.data.journal import Journal


def make_bt_bars(closes):
    return [BTBar(date=f"2026-01-{i+1:02d}", open=c, high=c, low=c, close=c, volume=1e6)
            for i, c in enumerate(closes)]


class TestBarStore:
    def test_save_and_load_roundtrip(self, tmp_path):
        store = BarStore(tmp_path / "bars.db")
        bars = [Bar("AAPL", "2026-01-01", 100, 101, 99, 100.5, 1e6),
                Bar("AAPL", "2026-01-02", 100.5, 102, 100, 101.5, 1.1e6)]
        assert store.save_bars(bars) == 2
        loaded = store.load_bars("AAPL")
        assert len(loaded) == 2
        assert loaded[0].close == 100.5

    def test_upsert_dedupes(self, tmp_path):
        store = BarStore(tmp_path / "bars.db")
        store.save_bars([Bar("AAPL", "2026-01-01", 100, 101, 99, 100.5, 1e6)])
        store.save_bars([Bar("AAPL", "2026-01-01", 100, 101, 99, 200.0, 1e6)])  # same key
        loaded = store.load_bars("AAPL")
        assert len(loaded) == 1
        assert loaded[0].close == 200.0  # updated

    def test_date_range_and_latest(self, tmp_path):
        store = BarStore(tmp_path / "bars.db")
        store.save_bars([Bar("AAPL", f"2026-01-{d:02d}", 1, 1, 1, 1, 1) for d in range(1, 11)])
        assert len(store.load_bars("AAPL", start="2026-01-05", end="2026-01-07")) == 3
        assert store.latest_date("AAPL") == "2026-01-10"
        assert store.symbols() == ["AAPL"]


class TestMetrics:
    def test_total_return_and_drawdown(self):
        curve = [100, 110, 90, 120]
        assert metrics.total_return(curve) == pytest.approx(0.2)
        # peak 110 -> trough 90 = 18.18% drawdown
        assert metrics.max_drawdown(curve) == pytest.approx((110 - 90) / 110)

    def test_sharpe_positive_for_steady_gains(self):
        curve = [100 * (1.01 ** i) for i in range(30)]  # steady 1%/period
        assert metrics.sharpe(metrics.returns_from_curve(curve)) > 0

    def test_beta_one_for_identical_series(self):
        rets = [0.01, -0.02, 0.03, 0.00, 0.015]
        alpha, beta = metrics.alpha_beta(rets, rets)
        assert beta == pytest.approx(1.0)
        assert alpha == pytest.approx(0.0, abs=1e-9)

    def test_beta_scales(self):
        bench = [0.01, -0.02, 0.03, -0.01]
        strat = [2 * r for r in bench]  # 2x leverage
        _, beta = metrics.alpha_beta(strat, bench)
        assert beta == pytest.approx(2.0)

    def test_compute_metrics_with_benchmark(self):
        strat = run_backtest(make_bt_bars([100 + i for i in range(60)]),
                             sma_crossover(5, 20), qty=10)
        bench = run_backtest(make_bt_bars([100 + i * 0.5 for i in range(60)]),
                             sma_crossover(5, 20), qty=10)
        m = metrics.compute_metrics(strat.equity_curve, bench.equity_curve)
        assert "Sharpe" in m.summary()


class TestWalkForward:
    def test_uptrend_passes(self):
        bars = make_bt_bars([100 + i for i in range(120)])
        wf = walk_forward(bars, sma_crossover(5, 20), n_folds=4)
        assert wf.n_folds == 4
        assert wf.passed()

    def test_insufficient_data_no_folds(self):
        wf = walk_forward(make_bt_bars([100, 101, 102]), sma_crossover(), n_folds=4)
        assert wf.n_folds == 0
        assert not wf.passed()

    def test_gate_promotes_candidate_on_pass(self, tmp_path):
        journal = Journal(tmp_path / "j.db")
        lifecycle.set_stage(journal, "trend", "candidate")
        bars = make_bt_bars([100 + i for i in range(120)])
        wf = walk_forward(bars, sma_crossover(5, 20), n_folds=4)
        status = gate_strategy(journal, "trend", wf)
        assert "promoted" in status
        assert lifecycle.get_stage(journal, "trend") == "paper"

    def test_gate_holds_candidate_on_fail(self, tmp_path):
        journal = Journal(tmp_path / "j.db")
        lifecycle.set_stage(journal, "choppy", "candidate")
        # A sawtooth that whipsaws the crossover -> poor OOS expectancy.
        closes = [100 + (5 if i % 2 == 0 else -5) for i in range(120)]
        wf = walk_forward(make_bt_bars(closes), sma_crossover(5, 20), n_folds=4)
        if not wf.passed():
            status = gate_strategy(journal, "choppy", wf)
            assert "stays candidate" in status
            assert lifecycle.get_stage(journal, "choppy") == "candidate"


class TestEquityCurve:
    def test_curve_has_one_point_per_bar(self):
        bars = make_bt_bars([100 + i for i in range(30)])
        result = run_backtest(bars, sma_crossover(5, 20), qty=10)
        assert len(result.equity_curve) == len(bars)

    def test_net_pnl_matches_curve_end(self):
        bars = make_bt_bars([100 + i for i in range(60)])
        result = run_backtest(bars, sma_crossover(5, 20), qty=10, starting_capital=10000)
        assert result.equity_curve[-1] == pytest.approx(10000 + result.net_pnl)

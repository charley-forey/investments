"""M19: Monte Carlo bootstrap, statistical edge report, benchmark comparison."""

import pytest

from backtest.montecarlo import bootstrap

from trading.analytics.edge import (
    benchmark_comparison, portfolio_edge, strategy_edge, strategy_edges,
)
from trading.analytics.scorer import score_closed_trades
from trading.data.bars import Bar, BarStore
from trading.data.journal import Journal


def seed_trades(journal, tag, pnls):
    """Seed scored trades directly via realized lots so scores exist."""
    for pnl in pnls:
        lot = journal.open_lot(symbol="X", qty=1, price=100.0, strategy_tag=tag)
        journal.close_lot(lot, price=100.0 + pnl)  # qty 1 -> realized == pnl
    score_closed_trades(journal)


class TestMonteCarlo:
    def test_all_winners_high_prob(self):
        mc = bootstrap([10.0] * 30, iterations=500, seed=1)
        assert mc.prob_profitable == 1.0
        assert mc.expectancy_p50 == pytest.approx(10.0)

    def test_mixed_gives_interval(self):
        mc = bootstrap([100, -50, 100, -50, 100, -50], iterations=1000, seed=1)
        assert mc.expectancy_p5 <= mc.expectancy_p50 <= mc.expectancy_p95
        assert 0.0 < mc.prob_profitable <= 1.0

    def test_empty(self):
        mc = bootstrap([], iterations=100)
        assert mc.prob_profitable == 0.0

    def test_deterministic_with_seed(self):
        a = bootstrap([1, -1, 2, -2, 3], iterations=200, seed=42)
        b = bootstrap([1, -1, 2, -2, 3], iterations=200, seed=42)
        assert a.expectancy_p50 == b.expectancy_p50


class TestEdge:
    def test_consistent_winner_is_proven(self, tmp_path):
        journal = Journal(tmp_path / "j.db")
        seed_trades(journal, "winner", [10.0] * 30)  # 30 identical wins -> significant
        e = strategy_edge(journal, "winner")
        assert e.trades == 30
        assert e.expectancy == pytest.approx(10.0)
        assert e.verdict == "proven"

    def test_few_trades_unproven(self, tmp_path):
        journal = Journal(tmp_path / "j.db")
        seed_trades(journal, "fresh", [10.0] * 5)  # below MIN_TRADES
        assert strategy_edge(journal, "fresh").verdict == "unproven"

    def test_consistent_loser_is_negative(self, tmp_path):
        journal = Journal(tmp_path / "j.db")
        seed_trades(journal, "loser", [-10.0] * 30)
        assert strategy_edge(journal, "loser").verdict == "negative"

    def test_noisy_breakeven_unproven(self, tmp_path):
        journal = Journal(tmp_path / "j.db")
        seed_trades(journal, "noise", [100, -100, 90, -95, 105, -100] * 5)  # ~0 mean, high var
        assert strategy_edge(journal, "noise").verdict == "unproven"

    def test_portfolio_gate(self, tmp_path):
        journal = Journal(tmp_path / "j.db")
        seed_trades(journal, "winner", [10.0] * 30)
        seed_trades(journal, "loser", [-10.0] * 30)
        pe = portfolio_edge(journal)
        assert pe["has_proven_edge"] is True
        assert pe["proven_strategies"] == ["winner"]

    def test_portfolio_gate_no_edge(self, tmp_path):
        journal = Journal(tmp_path / "j.db")
        seed_trades(journal, "fresh", [10.0] * 5)
        assert portfolio_edge(journal)["has_proven_edge"] is False


class TestBenchmark:
    def test_no_history(self, tmp_path):
        journal = Journal(tmp_path / "j.db")
        assert benchmark_comparison(journal, str(tmp_path / "bars.db"))["available"] is False

    def test_account_vs_spy(self, tmp_path):
        journal = Journal(tmp_path / "j.db")
        journal.record_equity(equity=100000, ts="2026-01-01T00:00:00+00:00")
        journal.record_equity(equity=110000, ts="2026-06-01T00:00:00+00:00")  # +10%
        bars_db = tmp_path / "bars.db"
        store = BarStore(bars_db)
        store.save_bars([Bar("SPY", "2026-01-01", 400, 400, 400, 400, 1),
                         Bar("SPY", "2026-06-01", 420, 420, 420, 420, 1)])  # +5%
        store.close()
        bc = benchmark_comparison(journal, str(bars_db))
        assert bc["account_return"] == pytest.approx(0.10)
        assert bc["benchmark_return"] == pytest.approx(0.05)
        assert bc["excess_return"] == pytest.approx(0.05)

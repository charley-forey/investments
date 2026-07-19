"""Scorer, per-strategy stats, and lifecycle gate transitions."""

from conftest import make_config

from trading.analytics import lifecycle
from trading.analytics.scorer import run_weekly, score_closed_trades
from trading.analytics.stats import compute_stats, stats_by_tag
from trading.data.journal import Journal


def seed_closed_trade(journal, symbol, tag, open_price, close_price, days_held=1):
    from datetime import datetime, timedelta, timezone

    open_ts = (datetime.now(timezone.utc) - timedelta(days=days_held)).isoformat()
    lot = journal.open_lot(symbol=symbol, qty=10, price=open_price, ts=open_ts,
                           strategy_tag=tag)
    journal.close_lot(lot, price=close_price)
    return lot


class TestScorer:
    def test_scores_closed_lots_once(self, tmp_path):
        journal = Journal(tmp_path / "j.db")
        seed_closed_trade(journal, "AAPL", "news-breakout", 100.0, 110.0)  # +100
        seed_closed_trade(journal, "MSFT", "news-breakout", 100.0, 95.0)   # -50

        report = score_closed_trades(journal)
        assert report.scored == 2
        assert report.gross_pnl == 100.0 * 1 - 50.0  # (110-100)*10 + (95-100)*10 = 100-50

        # Idempotent: a second run scores nothing new.
        assert score_closed_trades(journal).scored == 0
        assert len(journal.scores_for_tag("news-breakout")) == 2

    def test_untagged_lots_bucket(self, tmp_path):
        journal = Journal(tmp_path / "j.db")
        lot = journal.open_lot(symbol="AAPL", qty=10, price=100.0)
        journal.close_lot(lot, price=105.0)
        score_closed_trades(journal)
        assert journal.scores_for_tag("untagged")


class TestStats:
    def test_expectancy_and_winrate(self, tmp_path):
        journal = Journal(tmp_path / "j.db")
        config = make_config()
        seed_closed_trade(journal, "A", "t", 100.0, 110.0)   # +100
        seed_closed_trade(journal, "B", "t", 100.0, 110.0)   # +100
        seed_closed_trade(journal, "C", "t", 100.0, 90.0)    # -100
        score_closed_trades(journal)

        stats = compute_stats(journal.scores_for_tag("t"), config.settings.tax)
        assert stats.trades == 3
        assert stats.wins == 2
        assert stats.win_rate == 2 / 3
        assert stats.expectancy == (100 + 100 - 100) / 3  # +33.33...
        # after-tax expectancy is strictly less than gross for positive expectancy
        assert stats.after_tax_expectancy < stats.expectancy

    def test_max_drawdown(self, tmp_path):
        journal = Journal(tmp_path / "j.db")
        config = make_config()
        # curve: +100, -100, -100, +50 -> peak 100 then trough -100 -> DD 200
        for px in [110.0, 90.0, 90.0, 105.0]:
            seed_closed_trade(journal, "X", "t", 100.0, px)
        score_closed_trades(journal)
        stats = compute_stats(journal.scores_for_tag("t"), config.settings.tax)
        assert stats.max_drawdown == 200.0


class TestLifecycle:
    def test_default_stage_is_paper(self, tmp_path):
        journal = Journal(tmp_path / "j.db")
        assert lifecycle.get_stage(journal, "new-tag") == "paper"
        assert lifecycle.sizing_fraction(journal, "new-tag") == 1.0

    def test_promote_paper_to_live(self, tmp_path):
        journal = Journal(tmp_path / "j.db")
        config = make_config()
        # gates: 30 trades, expectancy > 0. Seed 30 small winners.
        for i in range(30):
            seed_closed_trade(journal, "A", "t", 100.0, 101.0)
        score_closed_trades(journal)
        changes = lifecycle.run_lifecycle(journal, config)
        assert any(c.new_stage == "small-live" for c in changes)
        assert lifecycle.get_stage(journal, "t") == "small-live"
        assert lifecycle.sizing_fraction(journal, "t") == 0.25

    def test_no_promotion_below_trade_bar(self, tmp_path):
        journal = Journal(tmp_path / "j.db")
        config = make_config()
        for i in range(5):  # far below the 30-trade gate
            seed_closed_trade(journal, "A", "t", 100.0, 101.0)
        score_closed_trades(journal)
        lifecycle.run_lifecycle(journal, config)
        assert lifecycle.get_stage(journal, "t") == "paper"

    def test_demote_after_losing_weeks(self, tmp_path):
        journal = Journal(tmp_path / "j.db")
        config = make_config()
        lifecycle.set_stage(journal, "t", "small-live")
        seed_closed_trade(journal, "A", "t", 100.0, 90.0)  # a loss this week
        score_closed_trades(journal)
        journal.set_state("losing_weeks:t", "2")  # at the demote threshold
        changes = lifecycle.run_lifecycle(journal, config)
        assert any(c.new_stage == "paper" for c in changes)
        assert lifecycle.get_stage(journal, "t") == "paper"

    def test_run_weekly_tracks_losing_streak(self, tmp_path):
        journal = Journal(tmp_path / "j.db")
        config = make_config()
        seed_closed_trade(journal, "A", "t", 100.0, 90.0)  # losing week
        score_closed_trades(journal)
        run_weekly(journal, config)
        assert journal.get_state("losing_weeks:t") == "1"

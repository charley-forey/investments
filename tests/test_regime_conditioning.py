"""Phase 3: regime-conditioned selection — edge lookup + prompt context."""

from __future__ import annotations

from trading.analytics.candidate_grading import (
    REGIME_MIN_N, regime_context, regime_edge,
)
from trading.data.journal import Journal


def _seed(journal, template, trend, vol, n, hit_frac):
    for i in range(n):
        sid = journal.record_snapshot(cycle="intraday", symbol=f"{template[:3]}{i}",
                                      bid=None, ask=None, last=10.0, spread_bps=None,
                                      features=None, sentiment=None, mention_count=None,
                                      template=template, trigger_direction="above",
                                      regime_trend=trend, regime_vol=vol)
        journal.record_candidate_outcome(
            snapshot_id=sid, symbol=f"{template[:3]}{i}", template=template,
            regime_trend=trend, regime_vol=vol, horizon_days=5, entry_price=10.0,
            forward_return=0.03, direction_right=(i < n * hit_frac))


class TestRegimeEdge:
    def test_none_below_min_sample(self, tmp_path):
        journal = Journal(tmp_path / "j.db")
        _seed(journal, "orb-breakout", "up", "calm", REGIME_MIN_N - 1, 0.2)
        assert regime_edge(journal, "orb-breakout", "up", "calm") is None

    def test_edge_reported_with_enough_sample(self, tmp_path):
        journal = Journal(tmp_path / "j.db")
        _seed(journal, "orb-breakout", "up", "calm", 30, 0.8)
        edge = regime_edge(journal, "orb-breakout", "up", "calm")
        assert edge["n"] == 30 and edge["hit_rate"] == 0.8

    def test_edge_is_regime_specific(self, tmp_path):
        journal = Journal(tmp_path / "j.db")
        _seed(journal, "orb-breakout", "up", "calm", 30, 0.8)     # works in calm uptrend
        _seed(journal, "orb-breakout", "down", "elevated", 30, 0.1)  # fails in down/vol
        good = regime_edge(journal, "orb-breakout", "up", "calm")
        bad = regime_edge(journal, "orb-breakout", "down", "elevated")
        assert good["hit_rate"] == 0.8 and bad["hit_rate"] < 0.45

    def test_unknown_regime_returns_none(self, tmp_path):
        journal = Journal(tmp_path / "j.db")
        _seed(journal, "orb-breakout", "up", "calm", 30, 0.8)
        assert regime_edge(journal, "orb-breakout", None, None) is None


class TestRegimeContext:
    def test_context_lists_templates_in_regime(self, tmp_path):
        journal = Journal(tmp_path / "j.db")
        _seed(journal, "trend-pullback-long", "up", "calm", 10, 0.7)
        _seed(journal, "breakout", "up", "calm", 10, 0.3)
        ctx = regime_context(journal, "up", "calm")
        assert "trend-pullback-long" in ctx and "breakout" in ctx and "up/calm" in ctx

    def test_retired_templates_are_not_shown_to_the_agent(self, tmp_path):
        """news-impulse and relative-strength-* were DELETED as bad signals (they
        graded -$553 and -$901). Their rows are still in the ledger, and unfiltered
        this told the agent every cycle that its three best setups in the current
        regime were things validate_tag mechanically rejects."""
        journal = Journal(tmp_path / "j.db")
        _seed(journal, "news-impulse", "up", "calm", 20, 0.9)
        _seed(journal, "relative-strength-short", "up", "calm", 20, 0.9)
        ctx = regime_context(journal, "up", "calm")
        assert ctx == "", f"retired templates leaked into the prompt: {ctx}"

    def test_return_is_signed_by_the_bet_direction(self, tmp_path):
        """A correct bearish call must read positive. Averaging raw forward returns
        across bullish and bearish candidates made hit rate and return contradict
        each other -- extended-from-sma read 'hit 87%, avg fwd -2.67%', which looks
        broken and was ten correct short calls."""
        journal = Journal(tmp_path / "j.db")
        for i in range(10):
            sid = journal.record_snapshot(
                cycle="intraday", symbol=f"BEAR{i}", bid=None, ask=None, last=10.0,
                spread_bps=None, features=None, sentiment=None, mention_count=None,
                template="breakout", trigger_direction="below",
                regime_trend="up", regime_vol="calm")
            journal.record_candidate_outcome(
                snapshot_id=sid, symbol=f"BEAR{i}", template="breakout",
                regime_trend="up", regime_vol="calm", horizon_days=5,
                entry_price=10.0, forward_return=-0.04, direction_right=True)
        ctx = regime_context(journal, "up", "calm")
        assert "+4.00%" in ctx, ctx

    def test_empty_when_no_data(self, tmp_path):
        journal = Journal(tmp_path / "j.db")
        assert regime_context(journal, "up", "calm") == ""

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
        _seed(journal, "orb-breakout", "up", "calm", 10, 0.7)
        _seed(journal, "news-impulse", "up", "calm", 10, 0.3)
        ctx = regime_context(journal, "up", "calm")
        assert "orb-breakout" in ctx and "news-impulse" in ctx and "up/calm" in ctx

    def test_empty_when_no_data(self, tmp_path):
        journal = Journal(tmp_path / "j.db")
        assert regime_context(journal, "up", "calm") == ""

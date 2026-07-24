"""Phase 1: shadow-grading scanner candidates by forward return + regime tagging."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd

from trading.analytics.candidate_grading import (
    grade_candidate, grade_pending_candidates, template_stats,
)
from trading.data.journal import Journal
from stubs import StubBroker, make_account


def _bars(start: str, closes: list[float]):
    base = datetime.fromisoformat(start.replace("Z", "+00:00")).date()
    idx = [base + timedelta(days=i + 1) for i in range(len(closes))]
    rows = [{"open": c, "high": c + 1, "low": c - 1, "close": c, "volume": 1e6} for c in closes]
    return pd.DataFrame(rows, index=pd.DatetimeIndex(idx, name="date"))


class TestGradeCandidate:
    def test_long_breakout_that_rose_is_right(self):
        row = {"id": 1, "symbol": "AAA", "last": 100.0, "template": "orb-breakout",
               "trigger_direction": "above", "regime_trend": "up", "regime_vol": "calm"}
        bars = [{"date": "2026-01-05", "close": 108.0, "open": 0, "high": 0, "low": 0}]
        out = grade_candidate(row, bars)
        assert out["forward_return"] == 0.08 and out["direction_right"] is True

    def test_short_that_fell_is_right(self):
        row = {"id": 2, "symbol": "BBB", "last": 50.0, "template": "relative-strength-short",
               "trigger_direction": "below", "regime_trend": "down", "regime_vol": "elevated"}
        bars = [{"date": "2026-01-05", "close": 46.0, "open": 0, "high": 0, "low": 0}]
        out = grade_candidate(row, bars)
        assert out["forward_return"] == -0.08 and out["direction_right"] is True

    def test_short_that_rose_is_wrong(self):
        row = {"id": 3, "symbol": "CCC", "last": 50.0, "template": "relative-strength-short",
               "trigger_direction": "below", "regime_trend": "up", "regime_vol": "calm"}
        bars = [{"date": "2026-01-05", "close": 55.0, "open": 0, "high": 0, "low": 0}]
        assert grade_candidate(row, bars)["direction_right"] is False

    def test_no_data_returns_none(self):
        row = {"id": 4, "symbol": "DDD", "last": 0.0, "template": "x", "trigger_direction": "above"}
        assert grade_candidate(row, [{"date": "2026-01-05", "close": 1.0}]) is None
        row["last"] = 10.0
        assert grade_candidate(row, []) is None


# Snapshots are backdated 6 days; bars must fall after that so there's a forward
# window to grade against.
_SNAP_TS = (datetime.now(timezone.utc) - timedelta(days=6)).isoformat()


class _GradeBroker(StubBroker):
    def __init__(self, closes_by_symbol):
        super().__init__(make_account())
        self._closes = closes_by_symbol

    def get_bars(self, symbol, days=30, timeframe="1Day"):
        return _bars(_SNAP_TS, self._closes[symbol])


def _old_snapshot(journal, **kw):
    sid = journal.record_snapshot(cycle="intraday", bid=None, ask=None,
                                  spread_bps=None, features=None, sentiment=None,
                                  mention_count=None, **kw)
    journal.conn.execute("UPDATE signal_snapshot SET ts=? WHERE id=?", (_SNAP_TS, sid))
    journal.conn.commit()
    return sid


class TestGradePending:
    def test_grades_only_candidates_and_is_idempotent(self, tmp_path):
        journal = Journal(tmp_path / "j.db")
        # A scanner candidate (has template) and a plain universe row (no template).
        _old_snapshot(journal, symbol="AAA", last=100.0, template="orb-breakout",
                      trigger_direction="above", trigger_level=99.0, cand_score=70.0,
                      regime_trend="up", regime_vol="calm")
        _old_snapshot(journal, symbol="ZZZ", last=200.0, template=None,
                      trigger_direction=None, regime_trend="up", regime_vol="calm")
        broker = _GradeBroker({"AAA": [100, 104, 108, 110, 112, 112], "ZZZ": [200] * 6})

        r1 = grade_pending_candidates(journal, broker)
        assert r1.graded == 1 and r1.right == 1  # only the candidate row, and it rose
        # Idempotent: nothing left to grade on a second pass.
        assert grade_pending_candidates(journal, broker).graded == 0

    def test_template_stats_by_regime(self, tmp_path):
        journal = Journal(tmp_path / "j.db")
        _old_snapshot(journal, symbol="AAA", last=100.0, template="orb-breakout",
                      trigger_direction="above", regime_trend="up", regime_vol="calm")
        _old_snapshot(journal, symbol="BBB", last=100.0, template="orb-breakout",
                      trigger_direction="above", regime_trend="down", regime_vol="elevated")
        broker = _GradeBroker({"AAA": [100, 105, 110, 110, 110, 110],   # +10% (right in 'up')
                               "BBB": [100, 95, 90, 90, 90, 90]})        # -10% (wrong in 'down')
        grade_pending_candidates(journal, broker)

        overall = {s["template"]: s for s in template_stats(journal)}
        assert overall["orb-breakout"]["n"] == 2
        by_regime = template_stats(journal, by_regime=True)
        up = next(s for s in by_regime if s["regime_trend"] == "up")
        down = next(s for s in by_regime if s["regime_trend"] == "down")
        assert up["hit_rate"] == 1.0 and down["hit_rate"] == 0.0

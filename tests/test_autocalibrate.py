"""Phase 2: bounded auto-calibration. The safety kernel (bounded_adjust) and the
gating/dry-run behaviour are the load-bearing parts, so they're tested directly."""

from __future__ import annotations

from conftest import make_config

from trading.analytics.autocalibrate import (
    Bound, SIZE_BOUND, WAKE_BOUND, WAKE_KEY, bounded_adjust,
    effective_wake_score, propose_wake_score, run_autocalibrate, size_multiplier,
)
from trading.data.journal import Journal


class TestBoundedAdjust:
    def test_small_n_barely_moves(self):
        # n=2 vs min_n=40 -> ~5% of the way, then clamped by max_step anyway.
        out = bounded_adjust(55.0, 80.0, WAKE_BOUND, n=2)
        assert abs(out - 55.0) <= 1.5

    def test_full_n_still_capped_by_max_step(self):
        # Large sample, big proposed jump -> moves exactly one max_step, no more.
        out = bounded_adjust(55.0, 80.0, WAKE_BOUND, n=1000)
        assert out == 55.0 + WAKE_BOUND.max_step

    def test_never_breaches_hard_bounds(self):
        assert bounded_adjust(WAKE_BOUND.hi, 999.0, WAKE_BOUND, n=1000) <= WAKE_BOUND.hi
        assert bounded_adjust(WAKE_BOUND.lo, -999.0, WAKE_BOUND, n=1000) >= WAKE_BOUND.lo

    def test_step_symmetric_downwards(self):
        out = bounded_adjust(70.0, 40.0, WAKE_BOUND, n=1000)
        assert out == 70.0 - WAKE_BOUND.max_step


def _grade_candidates(journal, n, hit_frac):
    """Insert n graded candidate outcomes with the given fraction 'right'."""
    for i in range(n):
        sid = journal.record_snapshot(cycle="intraday", symbol=f"S{i}", bid=None,
                                      ask=None, last=10.0, spread_bps=None, features=None,
                                      sentiment=None, mention_count=None,
                                      template="orb-breakout", trigger_direction="above")
        journal.record_candidate_outcome(
            snapshot_id=sid, symbol=f"S{i}", template="orb-breakout",
            regime_trend="up", regime_vol="calm", horizon_days=5, entry_price=10.0,
            forward_return=0.05, direction_right=(i < n * hit_frac))


class TestWakeProposal:
    def test_good_hit_rate_lowers_wake(self, tmp_path):
        journal = Journal(tmp_path / "j.db")
        _grade_candidates(journal, 50, hit_frac=0.8)   # strong signal -> surface more
        adj = propose_wake_score(journal, current=60.0)
        assert adj is not None and adj.new < 60.0

    def test_poor_hit_rate_raises_wake(self, tmp_path):
        journal = Journal(tmp_path / "j.db")
        _grade_candidates(journal, 50, hit_frac=0.2)   # noise -> be pickier
        adj = propose_wake_score(journal, current=60.0)
        assert adj is not None and adj.new > 60.0


class TestRunGating:
    def test_disabled_is_noop(self, tmp_path):
        cfg = make_config()
        cfg.settings.agents.auto_calibrate_enabled = False
        journal = Journal(tmp_path / "j.db")
        _grade_candidates(journal, 50, hit_frac=0.8)
        assert run_autocalibrate(cfg, journal) == []

    def test_under_min_outcomes_is_noop(self, tmp_path):
        cfg = make_config()
        cfg.settings.agents.auto_calibrate_enabled = True
        cfg.settings.agents.auto_calibrate_dry_run = False
        cfg.settings.agents.auto_calibrate_min_outcomes = 30
        journal = Journal(tmp_path / "j.db")
        _grade_candidates(journal, 5, hit_frac=0.8)    # below the gate
        assert run_autocalibrate(cfg, journal) == []
        assert journal.get_state(WAKE_KEY) is None      # nothing applied

    def test_dry_run_proposes_but_does_not_apply(self, tmp_path):
        cfg = make_config()
        cfg.settings.agents.auto_calibrate_enabled = True
        cfg.settings.agents.auto_calibrate_dry_run = True
        cfg.settings.agents.auto_calibrate_min_outcomes = 30
        journal = Journal(tmp_path / "j.db")
        _grade_candidates(journal, 50, hit_frac=0.8)
        changes = run_autocalibrate(cfg, journal)
        assert changes                                  # it proposes
        assert journal.get_state(WAKE_KEY) is None       # but applies nothing

    def test_apply_writes_clamped_kv(self, tmp_path):
        cfg = make_config()
        cfg.settings.agents.auto_calibrate_enabled = True
        cfg.settings.agents.auto_calibrate_dry_run = False
        cfg.settings.agents.auto_calibrate_min_outcomes = 30
        journal = Journal(tmp_path / "j.db")
        _grade_candidates(journal, 50, hit_frac=0.8)
        run_autocalibrate(cfg, journal)
        eff = effective_wake_score(cfg, journal)
        assert WAKE_BOUND.lo <= eff <= WAKE_BOUND.hi

    def test_size_multiplier_default_and_clamp(self, tmp_path):
        journal = Journal(tmp_path / "j.db")
        assert size_multiplier(journal, "news-impulse") == 1.0     # default
        journal.set_state("cal_size:news-impulse", "9.0")           # out of range
        assert size_multiplier(journal, "news-impulse") == SIZE_BOUND.hi

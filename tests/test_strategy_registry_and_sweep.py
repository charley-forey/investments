"""The strategy registry and the nightly backtest sweep.

Context: `strategy_tag` was unvalidated free text, so the agent invented tags that
became permanent ledger keys nothing could look up. The scanner emitted template names
for signals it did not compute (`news-impulse` = 3+ headlines and a 2% move). And the
`candidate -> backtest -> paper` ladder was dead code because nothing ever ran a
backtest against a live tag.
"""

from __future__ import annotations

from pathlib import Path

from trading import strategies as registry
from trading.analytics import lifecycle
from trading.analytics.sweep import _buy_hold_r, _exposure, _folds
from trading.data.journal import Journal


class _B:
    """Minimal bar."""

    def __init__(self, close, high=None, low=None):
        self.close = close
        self.high = high if high is not None else close
        self.low = low if low is not None else close
        self.open = close
        self.volume = 1000.0
        self.date = "2026-01-01"


class TestRegistry:
    def test_every_registered_signal_resolves(self):
        """A tag whose signal name is a typo would silently never be swept."""
        for strat in registry.backtestable():
            assert registry.signal_for(strat.tag) is not None, strat.tag

    def test_every_proposable_tag_has_a_playbook_file(self):
        """read_playbook dumps the whole directory, so the playbook set IS the menu
        the agent chooses from. A proposable tag with no playbook is invisible —
        which is exactly why trend-pullback-long was rarely traded."""
        pb = Path(__file__).resolve().parents[1] / "playbooks"
        for tag in registry.proposable_tags():
            strat = registry.get(tag)
            assert strat.playbook, f"{tag} has no playbook"
            assert (pb / f"{strat.playbook}.md").exists(), f"{tag}: missing playbook file"

    def test_deleted_templates_are_not_registered(self):
        for dead in ("news-impulse", "gap-and-go", "relative-strength-long",
                     "relative-strength-short", "orb-breakout"):
            assert registry.get(dead) is None

    def test_unknown_tag_is_rejected_with_a_usable_message(self):
        err = registry.validate_tag("trend-breakout-long")
        assert err and "unknown strategy_tag" in err
        # The error has to name the alternatives or the agent cannot self-correct.
        for tag in registry.proposable_tags():
            assert tag in err

    def test_known_tag_passes(self):
        assert registry.validate_tag("trend-pullback-long") is None

    def test_baseline_is_not_proposable(self):
        """sma-crossover exists to measure the others against, not to trade."""
        assert registry.get("sma-crossover").proposable is False
        assert registry.validate_tag("sma-crossover") is not None

    def test_options_bypass_the_enum(self):
        """propose_vertical synthesizes its own tags (debit-call-vertical); enforcing
        the stock enum on them would break the whole options path."""
        assert registry.validate_tag("debit-call-vertical", "option") is None


class TestSweepMetrics:
    def test_buy_hold_r_is_return_over_risk(self):
        bars = [_B(100.0), _B(110.0)]
        assert _buy_hold_r(bars) == 5.0     # +10% / 2% stop
        assert _buy_hold_r([_B(100.0)]) == 0.0

    def test_exposure_is_bounded_and_counts_held_bars(self):
        class T:
            def __init__(self, a, b):
                self.entry_idx, self.exit_idx = a, b

        assert _exposure([T(0, 10)], 100) == 0.1
        assert _exposure([T(0, 10), T(20, 30)], 100) == 0.2
        assert _exposure([], 100) == 0.0
        assert _exposure([T(0, 500)], 100) == 1.0    # clamped

    def test_folds_match_walkforward_split(self):
        from backtest.walkforward import walk_forward

        bars = [_B(100.0 + i) for i in range(100)]
        segs = _folds(bars, 4)
        assert len(segs) == 4
        assert sum(len(s) for s in segs) == 100     # no bar dropped or double-counted
        # Same split walk_forward uses, so alpha lines up with its fold results.
        wf = walk_forward(bars, lambda b, i: 0, n_folds=4)
        assert len(wf.folds) == len(segs)

    def test_gate_is_relative_not_absolute(self):
        """The first version of this gate passed all five strategies and ranked the
        deliberately dumb baseline top: a 2% stop over a decade of large-cap drift
        makes any long-biased rule look good. The benchmark, not zero, is the bar."""
        drifting = [_B(100.0 * (1.01 ** i)) for i in range(100)]
        assert _buy_hold_r(drifting) > 50, "a rising window must set a high bar"


class TestLifecycleGate:
    def test_default_is_unproven_at_quarter_size(self, tmp_path):
        j = Journal(tmp_path / "j.db")
        assert lifecycle.get_stage(j, "brand-new") == "unproven"
        assert lifecycle.sizing_fraction(j, "brand-new") == 0.25

    def test_backtest_promotion_stops_at_paper(self, tmp_path):
        """Automatic promotion must never reach real money. small-live/scaled stay
        with evaluate_tag (real closed trades) and the live approval gate."""
        j = Journal(tmp_path / "j.db")
        lifecycle.set_stage(j, "t", "paper")
        assert lifecycle.promote_after_backtest(j, "t", 99.0) is None
        lifecycle.set_stage(j, "t", "small-live")
        assert lifecycle.promote_after_backtest(j, "t", 99.0) is None
        assert lifecycle.get_stage(j, "t") == "small-live"

    def test_unproven_is_promotable_by_backtest(self, tmp_path):
        j = Journal(tmp_path / "j.db")
        change = lifecycle.promote_after_backtest(j, "t", 0.5)   # default: unproven
        assert change and change.old_stage == "unproven" and change.new_stage == "paper"

    def test_negative_alpha_does_not_promote(self, tmp_path):
        j = Journal(tmp_path / "j.db")
        assert lifecycle.promote_after_backtest(j, "t", -0.5) is None
        assert lifecycle.get_stage(j, "t") == "unproven"

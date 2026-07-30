"""The promotion gate measures the strategy we actually deploy, out of sample.

`mean_alpha_r` measures each strategy traded BLINDLY in every regime. That is not
the strategy this system runs -- regime_size_multiplier is live, so the deployed
strategy skips its losing cells. Gating on the blind number denied full size to
everything permanently (-1.70 to -2.50 measured), pinning every trade at the
`unproven` 0.25x forever: a 4x cap on profit that no improvement to trade
selection could lift.

But "slice 10 years, keep the cells that paid, report that they paid" is circular.
So the cell multipliers are fitted on folds 0..k-1 and scored on fold k.

Two subtleties this file pins, both of which produced wrong answers first:

  1. The fit must be POOLED across symbols, because that is what the live
     multiplier reads. Fitting per symbol leaves ~14 trades per cell -- under
     REGIME_MIN_TRADES -- so every cell falls back to full size and the
     "conditioned" number silently measures the unconditioned strategy.
  2. The breadth check must judge the DEPLOYED strategy. `positive_fraction`
     counts symbols where the blind strategy beat buy-and-hold, so reusing it
     re-imposes the criterion the gate exists to replace.
"""

import pytest

from trading.analytics.sweep import (
    MIN_OOS_FOLDS, REGIME_MIN_TRADES, REGIME_SKIP_BELOW_R, TagResult,
    _deployed_alpha_per_trade, _merge_acc, _walk_forward_oos,
)


def _cell(r, trades, bench_r, bars, held):
    return {"r": r, "trades": trades, "bench_r": bench_r, "bars": bars, "held": held}


def test_merge_sums_cells_across_folds():
    a = {"up/calm": _cell(10.0, 50, 4.0, 200, 60)}
    b = {"up/calm": _cell(5.0, 25, 2.0, 100, 30)}
    merged = _merge_acc([a, b])
    assert merged["up/calm"]["r"] == 15.0
    assert merged["up/calm"]["trades"] == 75


def test_a_losing_cell_learned_in_training_is_skipped_in_test():
    """The core mechanic: training says this cell loses, so its test trades are
    not taken and cannot drag the score down."""
    train = {"bad": _cell(-100.0, 200, 0.0, 400, 200)}      # -0.5 R/trade
    test = {"bad": _cell(-50.0, 100, 0.0, 200, 100)}
    alpha, taken, available = _deployed_alpha_per_trade(train, test)
    assert taken == 0 and available == 100
    assert alpha == 0.0


def test_a_winning_cell_is_taken_at_full_size():
    train = {"good": _cell(100.0, 200, 0.0, 400, 200)}
    test = {"good": _cell(60.0, 100, 0.0, 200, 100)}
    alpha, taken, _ = _deployed_alpha_per_trade(train, test)
    assert taken == 100
    assert alpha == pytest.approx(0.6)


def test_a_cell_unseen_in_training_is_taken_at_full_size():
    """Absence of evidence must not shrink positions -- same rule as live."""
    train = {"seen": _cell(10.0, 100, 0.0, 200, 100)}
    test = {"unseen": _cell(30.0, 100, 0.0, 200, 100)}
    alpha, taken, _ = _deployed_alpha_per_trade(train, test)
    assert taken == 100 and alpha == pytest.approx(0.3)


def test_a_thin_training_cell_cannot_gate():
    """Under REGIME_MIN_TRADES the training evidence is noise, so it must not be
    able to switch a cell off."""
    train = {"thin": _cell(-100.0, REGIME_MIN_TRADES - 1, 0.0, 200, 100)}
    test = {"thin": _cell(20.0, 100, 0.0, 200, 100)}
    _alpha, taken, _ = _deployed_alpha_per_trade(train, test)
    assert taken == 100, "a thin cell must not gate a strategy off"


def test_training_never_sees_the_fold_it_is_scored_on():
    """The anti-circularity property. Fold 1 is catastrophic; because fold 0 alone
    is what trains the gate for fold 1, the gate cannot have known."""
    folds = [
        {"c": _cell(50.0, 100, 0.0, 200, 100)},    # fold 0: cell looks great
        {"c": _cell(-90.0, 100, 0.0, 200, 100)},   # fold 1: it is not
    ]
    out = _walk_forward_oos(folds)
    assert out is not None
    alpha, folds_scored, taken, _available = out
    assert folds_scored == 1
    assert taken == 100, "training saw only fold 0, so the trade must be taken"
    assert alpha < 0, "and the loss must land in the score, not be hidden"


def test_no_out_of_sample_data_returns_none():
    assert _walk_forward_oos([{"c": _cell(1.0, 10, 0.0, 20, 10)}]) is None
    assert _walk_forward_oos([]) is None


def test_breadth_is_measured_on_the_deployed_strategy():
    """oos_positive_fraction, not positive_fraction: the blind count is what the
    gate is replacing, and using it made every strategy read 'fail' while scoring
    positive out of sample."""
    res = TagResult(tag="t", symbols_tested=100, symbols_positive=30,
                    symbols_positive_oos=70)
    assert res.positive_fraction == pytest.approx(0.30)
    assert res.oos_positive_fraction == pytest.approx(0.70)


def test_absent_oos_evidence_blocks_promotion():
    """No out-of-sample number must mean no promotion. Falling back to the pooled
    measure would reinstate the broken gate; falling back to 'pass' gives size
    away for free."""
    res = TagResult(tag="t", symbols_tested=80, mean_alpha_r=5.0,
                    oos_alpha_per_trade=None, oos_folds=0)
    assert res.oos_alpha_per_trade is None
    assert res.oos_folds < MIN_OOS_FOLDS
    assert not res.passed


def test_skip_threshold_is_inside_the_noise_band():
    """A cell that merely fails to beat the benchmark is a sizing question; only a
    clearly negative one is a skip."""
    assert REGIME_SKIP_BELOW_R < 0
    assert abs(REGIME_SKIP_BELOW_R) < 0.1

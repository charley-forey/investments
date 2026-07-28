"""Brackets, shorts and R-multiples in the backtester.

The point of these is that the live system now enforces a reward:risk floor
(limits.orders.min_reward_risk, now 2.5) that began as a guess from 20
hand-graded counterfactuals.
A backtester that cannot simulate a stop and a target cannot tell you whether that
number is right, so this is the machinery that turns the floor from a guess into
something falsifiable against 9 years of bars.
"""

import pytest

from backtest.engine import Bar, run_backtest


def bars(*rows):
    """rows are (open, high, low, close)."""
    return [Bar(date=f"d{i}", open=o, high=h, low=lo, close=c, volume=1000)
            for i, (o, h, lo, c) in enumerate(rows)]


def always(sig):
    return lambda bs, i: sig


def enter_then_hold(entry_idx=0):
    return lambda bs, i: 1 if i >= entry_idx else 0


# -- brackets ----------------------------------------------------------------

def test_stop_is_hit_intrabar_on_the_low_not_the_close():
    """A stop is a resting order. Scoring it at the close would flatter every
    strategy that gaps down and recovers."""
    b = bars((100, 101, 99, 100),      # entry at close 100, stop at 98
             (100, 101, 97, 100))      # low 97 pierces 98, close back at 100
    r = run_backtest(b, enter_then_hold(), stop_pct=0.02, qty=1.0)
    assert r.n == 1
    assert r.trades[0].exit_reason == "stop"
    assert r.trades[0].exit_price == pytest.approx(98.0)


def test_target_is_hit_intrabar_on_the_high():
    b = bars((100, 101, 99, 100),      # entry 100, stop 98, target 104 at 2R
             (100, 105, 100, 101))
    r = run_backtest(b, enter_then_hold(), stop_pct=0.02, target_r=2.0)
    assert r.trades[0].exit_reason == "target"
    assert r.trades[0].exit_price == pytest.approx(104.0)


def test_a_bar_that_spans_both_is_scored_as_the_loss():
    """Tick order inside a bar is unknowable. Taking the win is how backtests lie."""
    b = bars((100, 101, 99, 100),
             (100, 110, 90, 100))      # hits both the 98 stop and the 104 target
    r = run_backtest(b, enter_then_hold(), stop_pct=0.02, target_r=2.0)
    assert r.trades[0].exit_reason == "stop"


def test_without_stop_pct_behaviour_is_the_original_close_to_close():
    b = bars((100, 101, 90, 100), (100, 101, 90, 110))
    r = run_backtest(b, enter_then_hold())
    assert r.trades[0].exit_reason == "eod"
    assert r.trades[0].exit_price == pytest.approx(110.0)   # the low never mattered


# -- shorts ------------------------------------------------------------------

def test_shorts_are_ignored_unless_explicitly_allowed():
    """Existing long-flat signals must be unaffected by the new -1 path."""
    b = bars((100, 101, 99, 100), (100, 101, 99, 90))
    assert run_backtest(b, always(-1)).n == 0


def test_a_short_profits_when_price_falls():
    b = bars((100, 101, 99, 100), (100, 101, 89, 90))
    r = run_backtest(b, always(-1), allow_shorts=True)
    assert r.n == 1
    assert r.trades[0].direction == -1
    assert r.trades[0].gross_pnl == pytest.approx(10.0)     # 100 -> 90 short


def test_a_short_stop_is_above_entry():
    b = bars((100, 101, 99, 100),
             (100, 103, 100, 101))     # high 103 pierces the 102 stop
    r = run_backtest(b, always(-1), allow_shorts=True, stop_pct=0.02)
    assert r.trades[0].exit_reason == "stop"
    assert r.trades[0].exit_price == pytest.approx(102.0)


def test_a_reversal_closes_and_opens_in_one_bar():
    def sig(bs, i):
        return 1 if i < 2 else -1
    b = bars((100, 101, 99, 100), (100, 111, 99, 110),
             (110, 111, 109, 110), (110, 111, 99, 100))
    r = run_backtest(b, sig, allow_shorts=True)
    assert [t.direction for t in r.trades] == [1, -1]
    assert r.trades[0].exit_reason == "signal"


# -- R-multiples: the number the live floor is about -------------------------

def test_r_multiple_is_pnl_in_units_of_risk():
    b = bars((100, 101, 99, 100), (100, 105, 100, 101))
    r = run_backtest(b, enter_then_hold(), stop_pct=0.02, target_r=2.0,
                     spread_frac=0.0, slippage_bps=0.0)
    assert r.trades[0].r_multiple == pytest.approx(2.0)     # +4 on 2 of risk


def test_a_stopped_trade_is_about_minus_one_r():
    b = bars((100, 101, 99, 100), (100, 101, 97, 98))
    r = run_backtest(b, enter_then_hold(), stop_pct=0.02,
                     spread_frac=0.0, slippage_bps=0.0)
    assert r.trades[0].r_multiple == pytest.approx(-1.0)


def test_r_multiple_is_none_without_a_stop():
    b = bars((100, 101, 99, 100), (100, 101, 99, 110))
    r = run_backtest(b, enter_then_hold())
    assert r.trades[0].r_multiple is None
    assert r.expectancy_r is None


def test_expectancy_r_averages_across_trades_and_shows_in_summary():
    b = bars((100, 101, 99, 100), (100, 105, 100, 101))
    r = run_backtest(b, enter_then_hold(), stop_pct=0.02, target_r=2.0,
                     spread_frac=0.0, slippage_bps=0.0)
    assert r.expectancy_r == pytest.approx(2.0)
    assert "R/trade" in r.summary()


def test_exit_breakdown_counts_reasons():
    b = bars((100, 101, 99, 100), (100, 101, 97, 98))
    r = run_backtest(b, enter_then_hold(), stop_pct=0.02)
    assert r.exit_breakdown() == {"stop": 1}



# -- walk-forward carries the bracket through --------------------------------

def test_walk_forward_forwards_bracket_kwargs_and_reports_r():
    """The whole point of walk-forward here is validating GEOMETRY out of sample.
    If the kwargs were dropped it would silently validate an unbracketed strategy
    and report a confident number about something we do not trade."""
    from backtest.walkforward import walk_forward

    b = bars(*[(100 + i, 102 + i, 98 + i, 101 + i) for i in range(40)])
    wf = walk_forward(b, enter_then_hold(), n_folds=4,
                      stop_pct=0.02, target_r=2.0, spread_frac=0.0, slippage_bps=0.0)
    assert wf.n_folds == 4
    assert wf.mean_expectancy_r is not None
    assert any(f.expectancy_r is not None for f in wf.folds)


def test_walk_forward_reports_no_r_without_a_stop():
    from backtest.walkforward import walk_forward

    b = bars(*[(100 + i, 102 + i, 98 + i, 101 + i) for i in range(40)])
    wf = walk_forward(b, enter_then_hold(), n_folds=4)
    assert wf.mean_expectancy_r is None
    assert "R/trade" not in wf.summary()


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))

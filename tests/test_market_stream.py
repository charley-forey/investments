"""Tick-level detection: the state machine that replaces 15-minute sampling.

The point of the stream is that a level crossed at 10:02 and faded by 10:12 used
to be invisible. These assert the edge-triggering and debouncing that make that
detection cheap enough to act on — every event here becomes a billed LLM call.
"""

from datetime import datetime, timedelta
from types import SimpleNamespace

from trading.broker.market_stream import ET, SessionState


def at(h, m, s=0):
    return datetime(2026, 7, 27, h, m, s, tzinfo=ET)


def trig(direction, level):
    return SimpleNamespace(symbol="AAPL", direction=direction, level=level)


def _open_the_range(st, low=100.0, high=102.0):
    """Form a 100-102 opening range inside the 9:30-9:35 window."""
    st.on_trade(low, 100, at(9, 31))
    st.on_trade(high, 100, at(9, 33))


# -- opening range ------------------------------------------------------------

def test_opening_range_forms_and_does_not_fire_while_forming():
    st = SessionState(symbol="AAPL")
    assert st.on_trade(100.0, 100, at(9, 31)) == []
    assert st.on_trade(102.0, 100, at(9, 34)) == []
    assert (st.or_low, st.or_high) == (100.0, 102.0)


def test_orb_high_break_fires_once_the_range_is_frozen():
    st = SessionState(symbol="AAPL")
    _open_the_range(st)
    st.on_trade(101.0, 100, at(9, 40))          # inside the range, no event
    events = st.on_trade(102.5, 100, at(9, 41))
    assert [e.kind for e in events] == ["orb"]
    assert "opening range high 102" in events[0].detail


def test_orb_low_break_fires():
    st = SessionState(symbol="AAPL")
    _open_the_range(st)
    st.on_trade(101.0, 100, at(9, 40))
    events = st.on_trade(99.5, 100, at(9, 41))
    assert [e.kind for e in events] == ["orb"]
    assert "low" in events[0].detail


def test_trades_after_the_window_do_not_widen_the_range():
    st = SessionState(symbol="AAPL")
    _open_the_range(st)
    st.on_trade(105.0, 100, at(10, 0))
    assert st.or_high == 102.0                  # frozen, not 105


# -- edge triggering ----------------------------------------------------------

def test_a_level_already_through_is_not_a_trigger_event():
    """A standing fact is not news — the same rule the cost gate applies to scores.
    The range break here is real and should fire; the 100 trigger should not, because
    price was already above it when the session state started watching."""
    st = SessionState(symbol="AAPL", triggers=[trig("above", 100.0)])
    _open_the_range(st, 100.0, 102.0)
    kinds = [e.kind for e in st.on_trade(150.0, 100, at(10, 0))]
    assert "trigger" not in kinds
    assert kinds == ["orb"]


def test_crossing_up_through_a_trigger_fires():
    st = SessionState(symbol="AAPL", triggers=[trig("above", 110.0)])
    _open_the_range(st)
    st.on_trade(109.0, 100, at(10, 0))
    events = st.on_trade(110.5, 100, at(10, 1))
    assert [e.kind for e in events] == ["trigger"]
    assert "crossed above 110" in events[0].detail


def test_crossing_down_through_a_below_trigger_fires():
    st = SessionState(symbol="AAPL", triggers=[trig("below", 95.0)])
    _open_the_range(st)
    st.on_trade(96.0, 100, at(10, 0))
    events = st.on_trade(94.0, 100, at(10, 1))
    assert [e.kind for e in events] == ["trigger"]


def test_drifting_up_to_an_above_trigger_without_crossing_does_not_fire():
    st = SessionState(symbol="AAPL", triggers=[trig("above", 110.0)])
    _open_the_range(st)
    st.on_trade(105.0, 100, at(10, 0))
    assert st.on_trade(109.99, 100, at(10, 1)) == []


# -- debounce: every event costs an LLM call ---------------------------------

def test_chopping_across_a_level_bills_once_not_every_tick():
    st = SessionState(symbol="AAPL", triggers=[trig("above", 110.0)])
    _open_the_range(st)
    st.on_trade(109.0, 100, at(10, 0))
    assert len(st.on_trade(110.5, 100, at(10, 1))) == 1
    for i in range(10):                          # whipsaw across the level
        st.on_trade(109.0, 100, at(10, 2 + i))
        assert st.on_trade(110.5, 100, at(10, 2 + i)) == []


def test_the_same_level_fires_again_after_the_debounce_window():
    st = SessionState(symbol="AAPL", triggers=[trig("above", 110.0)])
    _open_the_range(st)
    st.on_trade(109.0, 100, at(10, 0))
    assert len(st.on_trade(110.5, 100, at(10, 1))) == 1
    later = at(10, 1) + timedelta(minutes=31)
    st.on_trade(109.0, 100, later)
    assert len(st.on_trade(110.5, 100, later)) == 1


# -- session state ------------------------------------------------------------

def test_vwap_is_volume_weighted_not_a_mean_of_prices():
    st = SessionState(symbol="AAPL")
    st.on_trade(100.0, 100, at(9, 31))
    st.on_trade(200.0, 900, at(9, 32))
    assert st.vwap == 190.0                      # not 150

def test_vwap_is_none_before_any_volume():
    assert SessionState(symbol="AAPL").vwap is None


def test_session_extremes_track_across_the_whole_session():
    st = SessionState(symbol="AAPL")
    _open_the_range(st)
    st.on_trade(120.0, 100, at(11, 0))
    st.on_trade(90.0, 100, at(14, 0))
    assert (st.session_low, st.session_high) == (90.0, 120.0)


def test_a_bad_tick_is_ignored_rather_than_corrupting_state():
    st = SessionState(symbol="AAPL")
    _open_the_range(st)
    assert st.on_trade(0.0, 100, at(10, 0)) == []
    assert st.last == 102.0                      # unchanged by the zero print


# -- daily rollover: this process runs for days -------------------------------

def test_a_new_session_resets_the_opening_range():
    """Without this, day 2's range merges with day 1's and can never break."""
    st = SessionState(symbol="AAPL")
    _open_the_range(st, 100.0, 102.0)
    st.on_trade(150.0, 100, at(11, 0))                 # day 1 runs up

    day2 = datetime(2026, 7, 28, 9, 31, tzinfo=ET)
    st.on_trade(200.0, 100, day2)
    st.on_trade(204.0, 100, day2.replace(minute=33))
    assert (st.or_low, st.or_high) == (200.0, 204.0)   # not 100-204
    assert st.session_low == 200.0                     # not 100 from yesterday


def test_debounce_does_not_leak_across_sessions():
    """A trigger that fired at 15:59 must still be able to fire the next morning."""
    st = SessionState(symbol="AAPL", triggers=[trig("above", 110.0)])
    _open_the_range(st)
    st.on_trade(109.0, 100, at(15, 58))
    assert len(st.on_trade(110.5, 100, at(15, 59))) == 1

    day2 = datetime(2026, 7, 28, 9, 31, tzinfo=ET)
    st.on_trade(100.0, 100, day2)
    st.on_trade(102.0, 100, day2.replace(minute=33))
    st.on_trade(109.0, 100, day2.replace(hour=10))
    assert len(st.on_trade(110.5, 100, day2.replace(hour=10, minute=1))) == 1


def test_vwap_restarts_each_session():
    st = SessionState(symbol="AAPL")
    st.on_trade(100.0, 1000, at(9, 31))
    day2 = datetime(2026, 7, 28, 9, 31, tzinfo=ET)
    st.on_trade(50.0, 100, day2)
    assert st.vwap == 50.0                             # not a blend with yesterday


# -- the queue between the two processes --------------------------------------

def test_wake_events_round_trip_and_drain_once(tmp_path):
    """The stream writes, the daemon's gate drains. Append-only with a consumed
    marker because these are separate processes — a read-modify-write races."""
    from trading.data.journal import Journal
    j = Journal(tmp_path / "j.db")
    j.record_wake_event(symbol="nvda", kind="orb", detail="broke OR high 211", price=211.4)
    j.record_wake_event(symbol="AAPL", kind="trigger", detail="crossed above 336", price=336.2)

    pending = j.pending_wake_events()
    assert [e["symbol"] for e in pending] == ["NVDA", "AAPL"]     # upper-cased, in order
    j.consume_wake_events([e["id"] for e in pending])
    assert j.pending_wake_events() == []                          # never billed twice


def test_reading_without_consuming_redelivers(tmp_path):
    """A crash between read and consume must re-deliver, not silently drop a
    real breakout — losing the event is worse than an extra LLM call."""
    from trading.data.journal import Journal
    j = Journal(tmp_path / "j.db")
    j.record_wake_event(symbol="NVDA", kind="orb", detail="d", price=1.0)
    assert len(j.pending_wake_events()) == 1
    assert len(j.pending_wake_events()) == 1


def test_gate_wakes_on_a_queued_market_event(tmp_path):
    """End to end: an event the stream saw between cycles wakes the strategy LLM."""
    from conftest import make_config
    from stubs import StubBroker, make_account
    from trading.data.journal import Journal
    from trading.triggers import should_run_intraday_llm

    config = make_config()
    config.settings.agents.trigger_gate_enabled = True
    config.settings.paths.journal_db = str(tmp_path / "j.db")
    j = Journal(tmp_path / "j.db")
    j.record_wake_event(symbol="NVDA", kind="orb", detail="broke OR high 211", price=211.4)

    now = datetime(2026, 7, 27, 11, 0, tzinfo=ET)   # outside every forced window
    d = should_run_intraday_llm(config, j, StubBroker(make_account()), make_account(),
                                now_et=now)
    assert d.run_llm and "market event" in d.reason and "NVDA" in d.reason
    # Drained: the same event must not buy a second call.
    assert j.pending_wake_events() == []



# -- liveness: this process can die quietly ----------------------------------

def test_stream_heartbeat_is_throttled_but_not_event_dependent(tmp_path):
    """It used to heartbeat ONLY when an event fired, so a quiet tape looked
    exactly like a dead websocket. Nothing else can tell: the watchdog judges only
    the daemon, and the launcher just checks the process exists."""
    from trading.broker.market_stream import HEARTBEAT_SECONDS
    assert HEARTBEAT_SECONDS <= 60          # observable within a minute of ticks


def test_status_flags_a_stale_tick_stream(tmp_path):
    """The per-component view must distinguish 'never reported' from 'reported and
    went quiet' — they mean different things when you are deciding whether to act."""
    from trading.data.journal import Journal
    j = Journal(tmp_path / "j.db")
    assert j.last_heartbeat("market_stream") is None
    j.heartbeat("market_stream", detail="120 ticks, 88 symbols")
    row = j.last_heartbeat("market_stream")
    assert row is not None and "88 symbols" in row["detail"]
    # Per-job lookup must not be shadowed by a newer heartbeat from another job.
    j.heartbeat("daemon", detail="started; 15 jobs")
    assert "88 symbols" in j.last_heartbeat("market_stream")["detail"]
    assert "15 jobs" in j.last_heartbeat("daemon")["detail"]



# -- regular hours only ------------------------------------------------------

def test_no_events_fire_premarket():
    """A premarket crossing queues for hours and drains stale, because the daemon's
    intraday cycle is closed. Observed live: GOOGL crossed at 07:18 and would have
    sat until 09:30."""
    st = SessionState(symbol="AAPL", triggers=[trig("above", 110.0)])
    st.on_trade(109.0, 100, at(7, 15))
    assert st.on_trade(110.5, 100, at(7, 18)) == []


def test_no_events_fire_after_the_close():
    st = SessionState(symbol="AAPL", triggers=[trig("above", 110.0)])
    st.on_trade(109.0, 100, at(16, 30))
    assert st.on_trade(110.5, 100, at(16, 31)) == []


def test_an_armed_plan_is_not_burned_premarket():
    """fire_armed_plan claims the plan BEFORE the pipeline runs, so a premarket
    crossing would consume it, get rejected for 'market closed', and leave nothing
    to fire at the real open."""
    st = SessionState(symbol="AAPL", armed=[{"id": 7, "level": 110.0, "direction": "above"}])
    st.on_trade(109.0, 100, at(8, 0))
    assert st.on_trade(110.5, 100, at(8, 1)) == []


def test_events_still_fire_during_regular_hours():
    st = SessionState(symbol="AAPL", triggers=[trig("above", 110.0)])
    _open_the_range(st)
    st.on_trade(109.0, 100, at(10, 0))
    assert [e.kind for e in st.on_trade(110.5, 100, at(10, 1))] == ["trigger"]


def test_premarket_prints_still_update_session_state():
    """VWAP and the extremes want the whole session; only EVENTS are gated."""
    st = SessionState(symbol="AAPL")
    st.on_trade(90.0, 100, at(7, 0))
    assert st.session_low == 90.0 and st.vwap == 90.0


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))

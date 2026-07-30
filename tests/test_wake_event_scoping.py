"""Regression for the 2026-07-29 re-deliberation loop.

124 intraday sessions, 1,049 tool calls, one unique trade idea, $14.40. Four
consecutive sessions at 16:27/16:28/16:29/16:30 UTC each reached the identical
conclusion: "flat; NET is at 266 vs its 277 breakout trigger; don't open into
FOMC." The tick stream had emitted 508 ORB events across all 88 streamed names to
provoke them.

Two properties fix that, and both are cheap deterministic filters that run before
any money is spent:

  1. An ARMED symbol needs no LLM. The plan fires from the tick stream through the
     full guardrail pipeline in milliseconds. Waking to re-derive the same entry
     is the loop itself.
  2. An ORB on a name the agent cannot act on is noise with a price tag.
"""

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from trading.data.journal import Journal
from trading.triggers import should_run_intraday_llm

from conftest import make_config
from stubs import StubBroker, make_account

ET = ZoneInfo("America/New_York")
QUIET = datetime(2026, 7, 27, 11, 0, tzinfo=ET)  # outside every forced window


def _gate(tmp_path, events, *, arm=None):
    config = make_config()
    config.settings.agents.trigger_gate_enabled = True
    config.settings.paths.journal_db = str(tmp_path / "j.db")
    j = Journal(tmp_path / "j.db")
    if arm:
        far_future = datetime(2099, 1, 1).isoformat()
        j.arm_plan(expires_at=far_future, proposal_json="{}", **arm)
    for sym, kind in events:
        j.record_wake_event(symbol=sym, kind=kind, detail="broke OR high", price=100.0)
    return should_run_intraday_llm(
        config, j, StubBroker(make_account()), make_account(), now_et=QUIET
    )


def test_orb_on_an_unwatched_name_does_not_buy_a_session(tmp_path):
    """ZZZZ is not held, armed, on the watchlist, a candidate, or in the core
    universe. 57 of the 88 streamed names were in exactly this position."""
    assert not _gate(tmp_path, [("ZZZZ", "orb")]).run_llm


def test_orb_on_a_core_name_still_wakes(tmp_path):
    """Coverage must not narrow to the scanner's 15-minute refresh."""
    d = _gate(tmp_path, [("AAPL", "orb")])
    assert d.run_llm and "AAPL" in d.reason


def test_explicit_trigger_events_are_never_scoped_away(tmp_path):
    """A `trigger` event is a level the agent named itself. Scoping applies to
    `orb` only -- dropping a named level would be losing a real decision."""
    d = _gate(tmp_path, [("ZZZZ", "trigger")])
    assert d.run_llm, "an agent-named level must always wake"


def test_an_armed_symbol_does_not_wake_the_llm(tmp_path):
    """The whole point: NET at 277 is decided. The stream executes it."""
    d = _gate(
        tmp_path,
        [("AAPL", "orb")],
        arm={"symbol": "AAPL", "level": 277.0, "direction": "above"},
    )
    assert not d.run_llm, "re-deliberating an armed plan is the $14.40 loop"


def test_routine_events_are_throttled_to_a_minimum_gap(tmp_path):
    """146 sessions/day came from any novel event in any minute buying a whole
    session. A 5-minute floor on the routine path takes that to ~55."""
    from trading.triggers import _LAST_LLM_TS_KEY, _MIN_SESSION_GAP_MINUTES
    from datetime import timedelta, timezone

    config = make_config()
    config.settings.agents.trigger_gate_enabled = True
    config.settings.paths.journal_db = str(tmp_path / "j.db")
    j = Journal(tmp_path / "j.db")
    just_now = datetime.now(timezone.utc) - timedelta(minutes=1)
    j.set_state(_LAST_LLM_TS_KEY, just_now.isoformat())
    j.record_wake_event(symbol="AAPL", kind="orb", detail="broke OR high", price=100.0)

    d = should_run_intraday_llm(config, j, StubBroker(make_account()),
                                make_account(), now_et=QUIET)
    assert not d.run_llm and "throttled" in d.reason


def test_a_named_trigger_bypasses_the_throttle(tmp_path):
    """Time-critical paths must not wait. A level the agent named itself is the
    thing it asked to be woken for."""
    from trading.triggers import _LAST_LLM_TS_KEY, save_triggers, Trigger
    from datetime import timedelta, timezone

    config = make_config()
    config.settings.agents.trigger_gate_enabled = True
    config.settings.paths.journal_db = str(tmp_path / "j.db")
    j = Journal(tmp_path / "j.db")
    j.set_state(_LAST_LLM_TS_KEY,
                (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat())
    j.record_wake_event(symbol="AAPL", kind="orb", detail="noise", price=100.0)
    save_triggers(config, [Trigger(symbol="SPY", direction="above", level=1.0)])

    broker = StubBroker(make_account())
    d = should_run_intraday_llm(config, j, broker, make_account(), now_et=QUIET)
    assert d.run_llm and "trigger hit" in d.reason


def test_arming_does_not_silence_a_different_symbol(tmp_path):
    """Suppression is per-symbol, not global -- an armed NET must not blind us
    to a genuine event on SPY."""
    d = _gate(
        tmp_path,
        [("SPY", "orb")],
        arm={"symbol": "AAPL", "level": 277.0, "direction": "above"},
    )
    assert d.run_llm and "SPY" in d.reason

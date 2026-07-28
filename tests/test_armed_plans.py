"""Pre-authorised orders: the LLM decides in advance, the stream executes in ms.

An LLM decision took a median 63s on 2026-07-27, which is the latency floor for
anything decided at the moment of the event. Arming moves the thinking earlier so
execution is mechanical.

That makes this the one path where an order reaches the broker with no LLM in the
loop, so these tests are about the safety properties: it runs the real guardrails,
it fires exactly once, and it refuses a trade that is no longer the one approved.
"""

from datetime import datetime, timedelta, timezone

import pytest
from conftest import make_config, make_quote
from stubs import StubBroker, make_account

from trading.broker.market_stream import ET, SessionState, fire_armed_plan
from trading.data.journal import Journal
from trading.guardrails.models import OrderProposal


def _plan_proposal(**kw):
    d = dict(symbol="AAPL", side="buy", qty=10, order_type="limit",
             limit_price=100.0, stop_price=98.0, target_price=104.0,
             expected_edge_usd=100.0, arm_level=100.0, arm_direction="above")
    d.update(kw)
    return OrderProposal(**d)


def _arm(journal, proposal, *, level=100.0, direction="above", hours=8):
    return journal.arm_plan(
        symbol=proposal.symbol, direction=direction, level=level,
        expires_at=(datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat(),
        proposal_json=proposal.model_dump_json(),
    )


def _setup(tmp_path):
    config = make_config()
    config.settings.paths.journal_db = str(tmp_path / "j.db")
    journal = Journal(tmp_path / "j.db")
    broker = StubBroker(make_account())
    return config, journal, broker


# -- the model flag ----------------------------------------------------------

def test_is_armed_plan_needs_both_level_and_direction():
    assert _plan_proposal().is_armed_plan
    assert not OrderProposal(symbol="AAPL", limit_price=100.0).is_armed_plan
    assert not _plan_proposal(arm_direction=None).is_armed_plan


# -- detection ---------------------------------------------------------------

def test_stream_emits_an_armed_event_on_the_crossing(tmp_path):
    st = SessionState(symbol="AAPL", armed=[{"id": 7, "level": 110.0, "direction": "above"}])
    st.on_trade(100.0, 100, datetime(2026, 7, 27, 9, 31, tzinfo=ET))
    st.on_trade(102.0, 100, datetime(2026, 7, 27, 9, 33, tzinfo=ET))
    st.on_trade(109.0, 100, datetime(2026, 7, 27, 10, 0, tzinfo=ET))
    events = st.on_trade(110.5, 100, datetime(2026, 7, 27, 10, 1, tzinfo=ET))
    armed = [e for e in events if e.kind == "armed"]
    assert len(armed) == 1 and "plan 7" in armed[0].detail


def test_armed_plans_are_not_debounced_like_triggers(tmp_path):
    """Debouncing a single-use plan would be redundant, and worse, could suppress a
    legitimate re-arm. The journal claim is what enforces single-use."""
    st = SessionState(symbol="AAPL", armed=[{"id": 7, "level": 110.0, "direction": "above"}])
    st.on_trade(100.0, 100, datetime(2026, 7, 27, 9, 31, tzinfo=ET))
    st.on_trade(102.0, 100, datetime(2026, 7, 27, 9, 33, tzinfo=ET))
    for i in range(3):
        st.on_trade(109.0, 100, datetime(2026, 7, 27, 10, i, tzinfo=ET))
        evs = st.on_trade(110.5, 100, datetime(2026, 7, 27, 10, i, tzinfo=ET))
        assert any(e.kind == "armed" for e in evs)


# -- single use --------------------------------------------------------------

def test_a_plan_can_only_be_claimed_once(tmp_path):
    _, journal, _ = _setup(tmp_path)
    pid = _arm(journal, _plan_proposal())
    assert journal.claim_armed_plan(pid, price=100.5) is True
    assert journal.claim_armed_plan(pid, price=100.6) is False   # the racing tick


def test_firing_twice_submits_once(tmp_path):
    config, journal, broker = _setup(tmp_path)
    pid = _arm(journal, _plan_proposal())
    plan = journal.active_armed_plans()[0]
    first = fire_armed_plan(config, journal, broker, plan, 100.5)
    second = fire_armed_plan(config, journal, broker, plan, 100.6)
    assert first == "submitted"
    assert second == "already claimed"
    assert len(broker.submitted) == 1


# -- the guardrails still run ------------------------------------------------

def test_firing_runs_the_real_guardrail_pipeline(tmp_path):
    """Approval earlier is not a licence to skip the mechanical checks. The fixture
    caps notional at $5,000; an armed plan for $100,000 must still be rejected."""
    config, journal, broker = _setup(tmp_path)
    _arm(journal, _plan_proposal(qty=1000, limit_price=100.0))
    plan = journal.active_armed_plans()[0]
    assert fire_armed_plan(config, journal, broker, plan, 100.5) == "rejected"
    assert broker.submitted == []


def test_firing_respects_the_reward_risk_floor(tmp_path):
    """The floor shipped alongside this. An armed plan is a proposal like any other."""
    config, journal, broker = _setup(tmp_path)
    _arm(journal, _plan_proposal(limit_price=100.0, stop_price=99.0, target_price=100.5))
    plan = journal.active_armed_plans()[0]
    assert fire_armed_plan(config, journal, broker, plan, 100.2) == "rejected"


# -- the trade must still be the one that was approved -----------------------

def test_a_gap_far_past_the_level_cancels_rather_than_chases(tmp_path):
    config, journal, broker = _setup(tmp_path)
    _arm(journal, _plan_proposal(), level=100.0)
    plan = journal.active_armed_plans()[0]
    out = fire_armed_plan(config, journal, broker, plan, 108.0)   # 8% through
    assert out.startswith("skipped")
    assert broker.submitted == []
    assert journal.active_armed_plans() == []                     # not left armed


def test_a_small_overshoot_still_fires(tmp_path):
    config, journal, broker = _setup(tmp_path)
    _arm(journal, _plan_proposal(), level=100.0)
    plan = journal.active_armed_plans()[0]
    assert fire_armed_plan(config, journal, broker, plan, 100.3) == "submitted"


# -- expiry ------------------------------------------------------------------

def test_an_expired_plan_is_never_returned_as_active(tmp_path):
    """Expiry is enforced on read, so a stale plan cannot fire even if the sweeper
    has not run — a breakout thesis from Tuesday is not a Thursday trade."""
    _, journal, _ = _setup(tmp_path)
    journal.arm_plan(
        symbol="AAPL", direction="above", level=100.0,
        expires_at=(datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(),
        proposal_json=_plan_proposal().model_dump_json(),
    )
    assert journal.active_armed_plans() == []


def test_expire_sweeper_marks_stale_plans(tmp_path):
    _, journal, _ = _setup(tmp_path)
    journal.arm_plan(
        symbol="AAPL", direction="above", level=100.0,
        expires_at=(datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(),
        proposal_json=_plan_proposal().model_dump_json(),
    )
    assert journal.expire_armed_plans() == 1
    assert journal.expire_armed_plans() == 0        # idempotent


def test_active_plans_filter_by_symbol(tmp_path):
    _, journal, _ = _setup(tmp_path)
    _arm(journal, _plan_proposal(symbol="AAPL"))
    _arm(journal, _plan_proposal(symbol="MSFT"))
    assert [p["symbol"] for p in journal.active_armed_plans("aapl")] == ["AAPL"]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))

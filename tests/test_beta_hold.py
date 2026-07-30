"""Index beta in up/calm, and nowhere else.

The evidence arrived twice, from different directions. The first sweep said no
strategy beats an exposure-matched passive hold. Then four structurally different
bets -- trend-long, trend-short, mean-reversion, short-vol -- each measured
NEGATIVE in up/calm over ~3,000 trades apiece. That cell is the most common of the
decade and had one of eight strategies available, the one with no backtest at all.

The cause is mechanical: the benchmark in that cell IS being long, so any rule
that sits in cash part of the time loses to it by construction. No fifth signal
fixes that.

These pin the properties that keep it narrow, which is the whole reason it is not
the passive core that was removed: one regime, no carve-outs, prompt exit.
"""

import pytest

from trading.analytics.beta_hold import (
    ACTIVE_REGIME, TAG, beta_proposal, plan_beta_hold,
)
from trading.broker.models import PositionView, Quote
from trading.guardrails.engine import OrderPipeline
from trading.guardrails.models import OrderProposal

from conftest import make_account
from stubs import StubBroker


@pytest.fixture
def cfg(config):
    bh = config.limits.beta_hold
    bh.enabled = True
    bh.symbol = "SPY"
    bh.target_pct = 30.0
    bh.rebalance_band_pct = 3.0
    return config


def _plan(cfg, journal, trend, vol, positions=None, equity=100_000, price=500.0):
    return plan_beta_hold(cfg, journal, make_account(equity=equity, positions=positions),
                          price, trend=trend, vol_state=vol)


def test_disabled_by_default(config, journal):
    config.limits.beta_hold.enabled = False
    assert _plan(config, journal, "up", "calm") is None


def test_holds_in_up_calm(cfg, journal):
    plan = _plan(cfg, journal, *ACTIVE_REGIME)
    assert plan.target_usd == pytest.approx(30_000)
    assert plan.target_shares == 60


@pytest.mark.parametrize("trend,vol", [
    ("up", "normal"), ("up", "elevated"), ("sideways", "calm"),
    ("sideways", "elevated"), ("down", "normal"), ("down", "elevated"),
])
def test_flat_in_every_other_regime(cfg, journal, trend, vol):
    """The narrowness IS the design. Anywhere else, the overlay has measured edge
    and this must not compete with it for capital."""
    plan = _plan(cfg, journal, trend, vol)
    assert plan.target_usd == 0.0
    assert plan.target_shares == 0


def test_leaving_the_regime_exits_immediately_not_on_the_band(cfg, journal):
    """A band exists to stop churn inside the regime. Leaving it is not drift --
    the edge is one cell wide and holding past it is holding on no evidence."""
    pos = PositionView(symbol="SPY", qty=60, avg_entry_price=500.0,
                       market_value=30_000.0, unrealized_pl=0.0)
    plan = _plan(cfg, journal, "sideways", "calm", positions=[pos])
    assert plan.target_shares == 0
    assert plan.delta == -60
    assert beta_proposal(plan, 500.0).side == "sell"


def test_small_drift_inside_the_regime_does_not_churn(cfg, journal):
    pos = PositionView(symbol="SPY", qty=59, avg_entry_price=500.0,
                       market_value=29_500.0, unrealized_pl=0.0)
    plan = _plan(cfg, journal, *ACTIVE_REGIME, positions=[pos])
    assert not plan.acts
    assert beta_proposal(plan, 500.0) is None


def test_unknown_regime_is_treated_as_not_up_calm(cfg, journal):
    assert _plan(cfg, journal, None, None).target_usd == 0.0


# -- no carve-outs: the reason this is not the passive core --------------------

def test_it_is_blocked_by_the_event_wall_like_any_other_stock_entry(cfg, journal,
                                                                   monkeypatch):
    """The passive core was exempt from the event wall, the lifecycle stage and the
    cost hurdle. This one carries no exemptions at all."""
    cfg.limits.events.block_stock_entry_within_days = 2
    cfg.limits.position.max_position_usd = 40_000
    cfg.limits.position.max_position_pct = 40
    cfg.limits.orders.max_order_notional_usd = 40_000
    monkeypatch.setattr("trading.data.calendar_feed.binary_events_within",
                        lambda *a, **k: ["FOMC 2026-07-30"])
    account = make_account(equity=100_000)
    plan = _plan(cfg, journal, *ACTIVE_REGIME)
    result = OrderPipeline(cfg, journal, StubBroker(account)).process(
        beta_proposal(plan, 500.0), account,
        Quote(symbol="SPY", bid=499.9, ask=500.1), market_is_open=True)
    assert result.status != "submitted"
    assert any(v.rule == "event_wall" for v in result.result.violations)


def test_it_carries_its_own_tag_so_the_ledger_can_judge_it(cfg, journal):
    plan = _plan(cfg, journal, *ACTIVE_REGIME)
    assert beta_proposal(plan, 500.0).strategy_tag == TAG

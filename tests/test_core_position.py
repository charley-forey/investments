"""The passive core: the book's default state.

40,212 backtested trades over 10 years say not one of the five strategies beats an
exposure-matched passive hold, and they are worst in the calm uptrends that
dominate the decade. The first 8 live days spent $63 of compute to realise $83
gross, most of it while flat. "Flat" was being treated as the safe default; it is
not, it is a guaranteed negative return equal to the burn rate.

These pin the properties that make holding beta safe: it is regime-scaled, it
rebalances on a band rather than on every wobble, it cannot crowd out the overlay,
and it goes through the same deterministic guardrails as everything else.
"""

import pytest

from trading.analytics.core_position import (
    CORE_TAG, core_proposal, plan_core_position,
)
from trading.broker.models import PositionView, Quote
from trading.guardrails.engine import OrderPipeline

from conftest import make_account


@pytest.fixture
def cfg(config):
    cp = config.limits.core_position
    cp.enabled = True
    cp.symbol = "SPY"
    cp.target_pct = 40.0
    cp.rebalance_band_pct = 5.0
    cp.max_share_of_gross_pct = 50.0
    return config


def test_disabled_by_default_is_a_no_op(config, journal):
    config.limits.core_position.enabled = False
    assert plan_core_position(config, journal, make_account(equity=100_000), 500.0) is None


def test_uptrend_gets_full_weight(cfg, journal):
    plan = plan_core_position(cfg, journal, make_account(equity=100_000), 500.0,
                              trend="up", vol_state="calm")
    assert plan.target_usd == pytest.approx(40_000)
    assert plan.target_shares == 80


def test_downtrend_cuts_the_core_hard(cfg, journal):
    """Asymmetric on purpose: holding beta into a drawdown costs far more than
    under-holding it in a rally."""
    plan = plan_core_position(cfg, journal, make_account(equity=100_000), 500.0,
                              trend="down", vol_state="elevated")
    assert plan.target_usd == pytest.approx(10_000)


def test_sideways_is_between(cfg, journal):
    plan = plan_core_position(cfg, journal, make_account(equity=100_000), 500.0,
                              trend="sideways", vol_state="calm")
    assert plan.target_usd == pytest.approx(24_000)


def test_small_drift_stays_inside_the_band(cfg, journal):
    """A passive core that rebalances on every wobble is an expensive active one."""
    pos = PositionView(symbol="SPY", qty=78, avg_entry_price=500.0,
                       market_value=39_000.0, unrealized_pl=0.0)
    plan = plan_core_position(cfg, journal,
                              make_account(equity=100_000, positions=[pos]), 500.0,
                              trend="up", vol_state="calm")
    assert not plan.acts, "1% drift must not trade"
    assert core_proposal(plan, 500.0) is None


def test_large_drift_rebalances(cfg, journal):
    pos = PositionView(symbol="SPY", qty=20, avg_entry_price=500.0,
                       market_value=10_000.0, unrealized_pl=0.0)
    plan = plan_core_position(cfg, journal,
                              make_account(equity=100_000, positions=[pos]), 500.0,
                              trend="up", vol_state="calm")
    assert plan.acts and plan.delta == 60
    proposal = core_proposal(plan, 500.0)
    assert proposal.side == "buy" and proposal.qty == 60
    assert proposal.strategy_tag == CORE_TAG


def test_core_cannot_crowd_out_the_overlay(cfg, journal):
    """Gross budget is shared. A core allowed to fill it would starve the signals
    it exists to complement."""
    cfg.limits.core_position.target_pct = 100.0
    cfg.limits.portfolio.max_gross_exposure_pct = 100.0
    cfg.limits.core_position.max_share_of_gross_pct = 50.0
    plan = plan_core_position(cfg, journal, make_account(equity=100_000), 500.0,
                              trend="up", vol_state="calm")
    assert plan.target_usd == pytest.approx(50_000), "must cap at half the gross budget"


def test_core_is_not_sized_down_by_the_unproven_lifecycle_stage(cfg, journal):
    """`passive-core` is an allocation, not a signal. It has no backtest to pass,
    so the 0.25x `unproven` stage must not apply -- otherwise the core silently
    holds a quarter of its target forever."""
    from stubs import StubBroker

    cfg.limits.position.max_position_usd = 50_000
    cfg.limits.position.max_position_pct = 60
    cfg.limits.orders.max_order_notional_usd = 50_000
    account = make_account(equity=100_000)
    plan = plan_core_position(cfg, journal, account, 500.0, trend="up", vol_state="calm")
    proposal = core_proposal(plan, 500.0)

    result = OrderPipeline(cfg, journal, StubBroker(account)).process(
        proposal, account, Quote(symbol="SPY", bid=499.9, ask=500.1),
        market_is_open=True)
    assert result.status == "submitted", result.result.violations


def test_core_is_exempt_from_the_event_wall(cfg, journal, monkeypatch):
    """An allocation is not a directional bet on a print, and binary events cover
    enough of the calendar that gating it would mean it never gets established."""
    from stubs import StubBroker

    cfg.limits.events.block_stock_entry_within_days = 2
    cfg.limits.position.max_position_usd = 50_000
    cfg.limits.position.max_position_pct = 60
    cfg.limits.orders.max_order_notional_usd = 50_000
    monkeypatch.setattr("trading.data.calendar_feed.binary_events_within",
                        lambda *a, **k: ["FOMC 2026-07-29"])
    account = make_account(equity=100_000)
    plan = plan_core_position(cfg, journal, account, 500.0, trend="up", vol_state="calm")

    result = OrderPipeline(cfg, journal, StubBroker(account)).process(
        core_proposal(plan, 500.0), account,
        Quote(symbol="SPY", bid=499.9, ask=500.1), market_is_open=True)
    assert result.status == "submitted", result.result.violations


def test_an_ordinary_stock_entry_is_still_event_blocked(cfg, journal, monkeypatch):
    """The exemption must be scoped to the core tag and nothing else."""
    from stubs import StubBroker
    from trading.guardrails.models import OrderProposal

    cfg.limits.events.block_stock_entry_within_days = 2
    monkeypatch.setattr("trading.data.calendar_feed.binary_events_within",
                        lambda *a, **k: ["FOMC 2026-07-29"])
    account = make_account(equity=100_000)
    result = OrderPipeline(cfg, journal, StubBroker(account)).process(
        OrderProposal(agent="strategy", strategy_tag="breakout", symbol="CRM",
                      asset_class="stock", side="buy", qty=10, order_type="limit",
                      limit_price=180.0, stop_price=176.0, thesis="t",
                      expected_edge_usd=100.0),
        account, Quote(symbol="CRM", bid=179.9, ask=180.1), market_is_open=True)
    assert result.status != "submitted"
    assert any(v.rule == "event_wall" for v in result.result.violations)

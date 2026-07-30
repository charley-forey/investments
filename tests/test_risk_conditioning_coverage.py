"""Every opening position gets the risk dials, not just long stock.

`_risk_size` used to return early on `side != "buy"` and on options. That was
harmless while the registry was long-stock-only. It stopped being harmless the
moment short tags and a push toward verticals landed: the two instruments the
system was told to start using were the two with no vol-targeting, no drawdown
throttle, no auto-calibration and no regime sizing on them.

An unconditioned short is the worst of these -- its loss is unbounded and gaps do
not respect stops.
"""

import json

import pytest

from trading.guardrails.models import OptionLeg, OrderProposal
from trading.orchestrator import CycleReport, Orchestrator

from conftest import make_account


def _seed_regime(journal, tag, per_trade, trades=1200):
    journal.set_state(f"backtest:{tag}", json.dumps({
        "mean_r": 0.4, "mean_alpha_r": -2.5, "trades": 8794,
        "symbols_tested": 88, "symbols_positive": 29, "passed": False,
        "regime": {"sideways/calm": {"alpha": per_trade * trades,
                                     "per_trade": per_trade, "trades": trades}},
    }))


@pytest.fixture
def orch(config, journal):
    config.limits.portfolio.vol_target_annual = 0.15
    return Orchestrator(config, journal, broker=None, client=None)


def _stock(side, tag="trend-pullback-long"):
    return OrderProposal(
        agent="strategy", strategy_tag=tag, symbol="AAPL", asset_class="stock",
        side=side, qty=1000, order_type="limit", limit_price=100.0,
        stop_price=95.0 if side == "buy" else 105.0,
        thesis="t", expected_edge_usd=500.0)


def _vertical(tag="debit-put-vertical", qty=10):
    return OrderProposal(
        agent="strategy", strategy_tag=tag, symbol="AAPL", asset_class="option",
        side="buy", qty=0, order_type="limit", thesis="t", expected_edge_usd=500.0,
        legs=[
            OptionLeg(side="buy", right="put", strike=100.0, expiry="2026-09-18",
                      qty=qty, est_premium=5.0),
            OptionLeg(side="sell", right="put", strike=95.0, expiry="2026-09-18",
                      qty=qty, est_premium=3.0),
        ])


def test_a_short_is_vol_sized_like_a_long(orch, journal, monkeypatch):
    """The gap that mattered: shorts skipped sizing entirely."""
    monkeypatch.setattr(orch, "_symbol_vol", lambda s: 0.60)  # high vol -> small size
    draft = _stock("sell")
    orch._risk_size(draft, make_account(equity=100_000), CycleReport(cycle="intraday"))
    assert draft.qty < 1000, "an unconditioned short is the worst position to allow"


def test_a_shorts_regime_multiplier_applies(orch, journal, monkeypatch):
    monkeypatch.setattr(orch, "_symbol_vol", lambda s: 0.20)
    _seed_regime(journal, "trend-pullback-short", -0.03)  # mild -> shrink, not gate
    report = CycleReport(cycle="intraday")
    draft = _stock("sell", tag="trend-pullback-short")
    orch._risk_size(draft, make_account(equity=100_000), report,
                    regime=("sideways", "calm"))
    assert any("regime" in n for n in report.notes), report.notes


def test_option_contracts_are_scaled_by_the_risk_dials(orch, journal):
    """Options are sized in CONTRACTS -- their risk is the net debit, not shares."""
    _seed_regime(journal, "debit-put-vertical", -0.04)
    report = CycleReport(cycle="intraday")
    draft = _vertical(qty=10)
    orch._risk_size(draft, make_account(equity=100_000), report,
                    regime=("sideways", "calm"))
    assert all(leg.qty < 10 for leg in draft.legs), "options bypassed conditioning"
    assert any("option-sized" in n for n in report.notes), report.notes


def test_both_legs_of_a_spread_scale_together(orch, journal):
    """Scaling one side of a vertical turns it into a different structure."""
    _seed_regime(journal, "debit-put-vertical", -0.04)
    draft = _vertical(qty=8)
    orch._risk_size(draft, make_account(equity=100_000), CycleReport(cycle="intraday"),
                    regime=("sideways", "calm"))
    assert draft.legs[0].qty == draft.legs[1].qty


def test_a_spread_never_scales_below_one_contract(orch, journal):
    """A structure scaled to zero contracts is malformed, not small. The regime
    GATE handles the genuinely unwanted case upstream by vetoing it outright."""
    _seed_regime(journal, "debit-put-vertical", -0.049)
    draft = _vertical(qty=1)
    orch._risk_size(draft, make_account(equity=100_000), CycleReport(cycle="intraday"),
                    regime=("sideways", "calm"))
    assert all(leg.qty >= 1 for leg in draft.legs)


def test_exits_are_never_resized(orch, journal, monkeypatch):
    """Sizing must never touch a position-reducing order -- shrinking an exit
    leaves part of the position stranded."""
    monkeypatch.setattr(orch, "_symbol_vol", lambda s: 0.90)
    draft = _stock("sell")
    draft.reduces_position = True
    orch._risk_size(draft, make_account(equity=100_000), CycleReport(cycle="intraday"))
    assert draft.qty == 1000

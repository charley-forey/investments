"""Regression for the 2026-07-29 CRM liquidation.

The agent tried three times to put a protective stop under its only open
position. The proposal schema had no 'stop' order type, so all three came out as
sell LIMIT orders. The risk agent caught the defect on #32 ("a sell limit at 178
against a 183 bid... would fill immediately near the market"), vetoed it, and
then approved the identical structure on #33 at a worse level. It filled
instantly at 182.94 against a 172 limit and sold the position it was meant to
protect. It happened to be profitable, which is why nothing flagged it.

The lesson is that this cannot be left to the reviewer's judgement -- the
reviewer had already demonstrated it could reason correctly about the defect and
approve it anyway. These are the deterministic gates.
"""

import pytest

from trading.broker.models import PositionView, Quote
from trading.guardrails.engine import OrderPipeline
from trading.guardrails.models import OrderProposal

from conftest import make_account
from stubs import StubBroker


CRM_QUOTE = Quote(symbol="CRM", bid=183.09, ask=183.42)


def _crm_account():
    pos = PositionView(symbol="CRM", qty=49, avg_entry_price=179.39,
                       market_value=8971.41, unrealized_pl=153.0)
    return make_account(equity=100078.0, positions=[pos])


def _exit(**kw):
    base = dict(
        agent="strategy", strategy_tag="relative-strength-long", symbol="CRM",
        asset_class="stock", side="sell", qty=49, reduces_position=True,
        thesis="protective stop into FOMC", expected_edge_usd=0.0,
    )
    base.update(kw)
    return OrderProposal(**base)


def _process(config, journal, proposal, account=None, quote=CRM_QUOTE):
    broker = StubBroker(account or _crm_account())
    pipeline = OrderPipeline(config, journal, broker=broker)
    return pipeline.process(proposal, account or _crm_account(), quote,
                            market_is_open=True), broker


# -- the exact shape of proposal #33 ------------------------------------------

def test_the_crm_33_defect_is_rejected(config, journal):
    """A 'limit' order carrying a stop_price is a mis-typed protective stop."""
    result, broker = _process(config, journal, _exit(
        order_type="limit", limit_price=172.0, stop_price=173.0,
    ))
    assert result.status != "submitted"
    assert any("stop_geometry" == v.rule for v in result.result.violations), \
        result.result.violations
    assert broker.submitted == [], "nothing may reach the broker"


def test_a_correct_protective_stop_is_accepted_and_rests(config, journal):
    """Same intent, right order type: rests below the bid, does not fill now."""
    result, broker = _process(config, journal, _exit(
        order_type="stop", stop_price=173.0,
    ))
    assert result.status == "submitted", result.result.violations
    sent = broker.submitted[-1]
    assert sent["order_type"] == "stop"
    # The trigger must reach the broker as the stop price. Before the fix this
    # was None and alpaca.submit_order fell back to limit_price, which a resting
    # stop need not carry at all.
    assert sent["stop_loss_price"] == 173.0


# -- geometry: a stop on the wrong side of the book is a market order ---------

def test_sell_stop_at_or_above_the_bid_is_rejected(config, journal):
    """173 is fine against a 183 bid; 183.50 would trigger the instant it rests."""
    result, _ = _process(config, journal, _exit(order_type="stop", stop_price=183.50))
    assert result.status != "submitted"
    assert any("stop_geometry" == v.rule for v in result.result.violations)


def test_buy_stop_at_or_below_the_ask_is_rejected(config, journal):
    """Mirror case, so a short cover cannot be mis-armed the same way."""
    result, _ = _process(config, journal, _exit(
        side="buy", order_type="stop", stop_price=183.00, reduces_position=True,
    ))
    assert result.status != "submitted"
    assert any("stop_geometry" == v.rule for v in result.result.violations)


def test_stop_order_without_a_trigger_is_rejected(config, journal):
    result, _ = _process(config, journal, _exit(order_type="stop", stop_price=None))
    assert result.status != "submitted"


# -- and the legitimate case must still work ----------------------------------

def test_an_intentional_immediate_exit_still_passes(config, journal):
    """A marketable sell limit with no stop_price is a deliberate 'out now' and
    must not be caught by the mis-typed-stop rule -- that is how every real exit
    in the ledger was filled."""
    result, broker = _process(config, journal, _exit(
        order_type="limit", limit_price=182.94,
    ))
    assert result.status == "submitted", result.result.violations
    assert broker.submitted[-1]["order_type"] == "limit"


def test_opening_buy_still_brackets_off_stop_price(config, journal):
    """An opening trade uses stop_price for the bracket leg, not as a trigger;
    the new rule keys on reduces_position so it must not touch this path."""
    account = make_account(equity=100000.0)
    # qty kept under the test fixture's $5,000 notional cap; the point of this
    # test is the bracket leg, not sizing.
    result, broker = _process(config, journal, OrderProposal(
        agent="strategy", strategy_tag="relative-strength-long", symbol="CRM",
        asset_class="stock", side="buy", qty=25, order_type="limit",
        limit_price=179.70, stop_price=176.40, target_price=188.0,
        reduces_position=False, thesis="breakout", expected_edge_usd=400.0,
    ), account=account)
    assert result.status == "submitted", result.result.violations
    assert broker.submitted[-1]["stop_loss_price"] == 176.40

"""Regressions for the 2026-07-22 session failures.

Each test pins one defect that cost real money or real visibility that day:
positions going naked overnight, exits that could not be submitted, one broker
rejection killing a whole cycle, and an unquotable book reading as a tight one.
"""

from dataclasses import dataclass

import pytest

from trading.broker.models import PositionView, Quote
from trading.broker.sync import ensure_protective_stops, release_held_orders
from trading.guardrails.models import OrderProposal

from conftest import make_account, make_config
from stubs import StubBroker


@dataclass
class FakeOrder:
    id: str
    symbol: str
    side: str
    order_type: str = "limit"


class OrderBroker(StubBroker):
    """StubBroker with a controllable open-order book."""

    def __init__(self, *args, open_orders=None, **kw):
        super().__init__(*args, **kw)
        self._open_orders = open_orders or []
        self.canceled: list[str] = []

    def cancel_order(self, order_id):
        self.canceled.append(order_id)
        self._open_orders = [o for o in self._open_orders if str(o.id) != str(order_id)]


# -- released shares: the exit that failed at 14:37 ---------------------------

def test_release_cancels_protective_legs_on_the_closing_side():
    broker = OrderBroker(make_account(), open_orders=[
        FakeOrder("stop-leg", "SMCI", "sell", "stop"),
        FakeOrder("target-leg", "SMCI", "sell", "limit"),
    ])
    assert release_held_orders(broker, "SMCI", "sell") == 2
    assert set(broker.canceled) == {"stop-leg", "target-leg"}


def test_release_leaves_other_symbols_and_opening_orders_alone():
    broker = OrderBroker(make_account(), open_orders=[
        FakeOrder("other-symbol", "AAPL", "sell", "stop"),
        FakeOrder("an-entry", "SMCI", "buy", "limit"),
    ])
    assert release_held_orders(broker, "SMCI", "sell") == 0
    assert broker.canceled == []


def test_reducing_proposal_frees_shares_before_submitting(config, journal):
    """The 7/22 failure end to end: a bracket held all 159 shares, so the close
    was rejected with 'insufficient qty available'. The legs must go first."""
    from trading.guardrails.engine import OrderPipeline

    pos = PositionView(symbol="SMCI", qty=159, avg_entry_price=31.25,
                       market_value=4968.75, unrealized_pl=-80.0)
    account = make_account(equity=100000.0, positions=[pos])
    broker = OrderBroker(account, open_orders=[
        FakeOrder("held-stop", "SMCI", "sell", "stop"),
    ])
    pipeline = OrderPipeline(config, journal, broker=broker)
    proposal = OrderProposal(
        agent="exit_manager", strategy_tag="deterministic_exit", symbol="SMCI",
        asset_class="stock", side="sell", qty=159, order_type="limit",
        limit_price=30.82, reduces_position=True, thesis="close", expected_edge_usd=0.0,
    )

    result = pipeline.process(proposal, account, Quote(symbol="SMCI", bid=30.80, ask=30.84),
                              market_is_open=True)

    assert result.status == "submitted"
    assert broker.canceled == ["held-stop"], "protective leg must be cancelled first"
    assert broker.submitted[-1]["side"] == "sell"


# -- never naked overnight ----------------------------------------------------

def test_protective_sweep_arms_an_unstopped_position(journal):
    config = make_config()
    config.limits.exits.stop_loss_pct = 8.0
    pos = PositionView(symbol="SMCI", qty=159, avg_entry_price=31.25,
                       market_value=4968.75, unrealized_pl=-80.0)
    broker = OrderBroker(make_account(positions=[pos]), open_orders=[])

    assert ensure_protective_stops(config, journal, broker) == 1
    order = broker.submitted[-1]
    assert order["order_type"] == "stop" and order["side"] == "sell"
    assert order["stop_loss_price"] == pytest.approx(31.25 * 0.92, abs=0.01)


def test_protective_sweep_is_idempotent_when_a_stop_exists(journal):
    config = make_config()
    config.limits.exits.stop_loss_pct = 8.0
    pos = PositionView(symbol="SMCI", qty=159, avg_entry_price=31.25,
                       market_value=4968.75, unrealized_pl=-80.0)
    broker = OrderBroker(make_account(positions=[pos]),
                         open_orders=[FakeOrder("live-stop", "SMCI", "sell", "stop")])

    assert ensure_protective_stops(config, journal, broker) == 0
    assert broker.submitted == []


def test_bracket_orders_are_gtc_so_protection_survives_the_close():
    """A DAY bracket expires its target at the close, which cancels the sibling
    stop and leaves the position unprotected overnight."""
    from alpaca.trading.enums import TimeInForce
    from alpaca.trading.requests import LimitOrderRequest

    captured = {}

    class Recorder:
        def submit_order(self, order_data=None):
            captured["req"] = order_data
            return type("O", (), {"id": "x"})()

    config = make_config()
    from trading.broker.alpaca import AlpacaBroker

    broker = AlpacaBroker.__new__(AlpacaBroker)
    broker.config = config
    broker._trading = Recorder()

    broker.submit_order(symbol="SMCI", side="buy", qty=159, order_type="limit",
                        limit_price=31.27, stop_loss_price=30.0, take_profit_price=34.5)
    assert isinstance(captured["req"], LimitOrderRequest)
    assert captured["req"].time_in_force == TimeInForce.GTC

    # A plain entry with no protection attached stays a DAY order.
    broker.submit_order(symbol="AAPL", side="buy", qty=10, order_type="limit",
                        limit_price=100.0)
    assert captured["req"].time_in_force == TimeInForce.DAY


# -- an unquotable book must never look tight ---------------------------------

def test_one_sided_book_reports_infinite_spread():
    """IEX returns ask=0 for liquid names; reporting that as spread 0.0 let an
    unpriceable trade sail through the cost hurdle."""
    assert Quote(symbol="XOM", bid=145.39, ask=0.0).spread == float("inf")
    assert Quote(symbol="XOM", bid=145.39, ask=0.0).is_two_sided is False
    good = Quote(symbol="XOM", bid=145.39, ask=145.45)
    assert good.is_two_sided is True
    assert good.spread == pytest.approx(0.06)


def test_cost_hurdle_rejects_an_unquotable_book(config, journal):
    from trading.guardrails.engine import OrderPipeline

    account = make_account(equity=100000.0)
    pipeline = OrderPipeline(config, journal, broker=None)
    proposal = OrderProposal(
        agent="strategy", strategy_tag="test", symbol="XOM", asset_class="stock",
        side="buy", qty=10, order_type="limit", limit_price=145.40,
        thesis="t", expected_edge_usd=500.0,
    )
    result = pipeline.process(proposal, account, Quote(symbol="XOM", bid=145.39, ask=0.0),
                              market_is_open=True)
    assert result.status != "submitted"


# -- one rejection must not kill the cycle ------------------------------------

def test_rolling_cache_breakpoint_marks_only_the_latest_block():
    from trading.agents.runner import _roll_cache_breakpoint

    messages = [
        {"role": "user", "content": "start"},
        {"role": "user", "content": [{"type": "tool_result", "content": "a"}]},
    ]
    _roll_cache_breakpoint(messages)
    assert messages[1]["content"][-1]["cache_control"] == {"type": "ephemeral"}

    messages.append({"role": "user", "content": [{"type": "tool_result", "content": "b"}]})
    _roll_cache_breakpoint(messages)
    assert "cache_control" not in messages[1]["content"][-1], "old breakpoint must move"
    assert messages[2]["content"][-1]["cache_control"] == {"type": "ephemeral"}

"""M4: OCC symbols, options execution, live stage gate, async approval."""

from datetime import date, timedelta

import pytest

from conftest import make_config, make_quote

from stubs import StubBroker, make_account

from trading.analytics import lifecycle
from trading.broker.occ import build_occ, parse_occ
from trading.data.journal import Journal
from trading.guardrails.engine import OrderPipeline
from trading.guardrails.models import OptionLeg, OrderProposal


def far() -> date:
    return date.today() + timedelta(days=45)


class TestOcc:
    def test_build_and_parse_round_trip(self):
        occ = build_occ("AAPL", date(2026, 1, 16), "call", 190.0)
        assert occ == "AAPL260116C00190000"
        parts = parse_occ(occ, underlying="AAPL")
        assert parts.underlying == "AAPL"
        assert parts.expiry == date(2026, 1, 16)
        assert parts.right == "call"
        assert parts.strike == 190.0

    def test_parse_without_underlying_hint(self):
        occ = build_occ("SPY", date(2026, 3, 20), "put", 500.0)
        parts = parse_occ(occ)
        assert parts.underlying == "SPY"
        assert parts.right == "put"
        assert parts.strike == 500.0

    def test_fractional_strike(self):
        occ = build_occ("F", date(2026, 6, 19), "call", 12.5)
        assert parse_occ(occ, "F").strike == 12.5

    def test_malformed_raises(self):
        with pytest.raises(ValueError):
            parse_occ("NOTVALID")


def option_proposal(legs, **kw) -> OrderProposal:
    defaults = dict(symbol="AAPL", asset_class="option", order_type="limit",
                    expected_edge_usd=200.0, strategy_tag="test", legs=legs)
    defaults.update(kw)
    return OrderProposal(**defaults)


class OptionBroker(StubBroker):
    """Stub that records multi-leg option submissions and returns option quotes."""

    def __init__(self, account, **kw):
        super().__init__(account, **kw)
        self.option_orders = []

    def submit_option_order(self, *, legs, net_limit_price, underlying, client_order_id=None):
        self.option_orders.append(
            dict(legs=legs, net_limit_price=net_limit_price, underlying=underlying,
                 client_order_id=client_order_id)
        )
        return f"opt-order-{len(self.option_orders)}"

    def get_option_quote(self, occ):
        return make_quote(symbol=occ, bid=1.95, ask=2.05)  # $0.10 spread


class TestOptionsExecution:
    def test_debit_spread_submits_to_broker(self, tmp_path):
        config = make_config()
        journal = Journal(tmp_path / "j.db")
        broker = OptionBroker(make_account())
        pipeline = OrderPipeline(config, journal, broker)

        legs = [
            OptionLeg(side="buy", right="call", strike=100, expiry=far(), qty=1, est_premium=5.0),
            OptionLeg(side="sell", right="call", strike=105, expiry=far(), qty=1, est_premium=3.0),
        ]
        res = pipeline.process(option_proposal(legs), make_account(), make_quote(),
                               market_is_open=True)
        assert res.status == "submitted"
        assert len(broker.option_orders) == 1
        order = broker.option_orders[0]
        assert len(order["legs"]) == 2
        # net debit = 5 - 3 = 2.00
        assert order["net_limit_price"] == pytest.approx(2.0)
        # OCC symbols were constructed for each leg
        assert all("occ_symbol" in leg for leg in order["legs"])

    def test_real_leg_spreads_used_in_cost_hurdle(self, tmp_path):
        config = make_config()
        journal = Journal(tmp_path / "j.db")
        broker = OptionBroker(make_account())
        pipeline = OrderPipeline(config, journal, broker)
        legs = [OptionLeg(side="buy", right="call", strike=100, expiry=far(),
                          qty=1, est_premium=5.0)]
        # real spread $0.10/share x 100 = $10 spread cost; edge must clear 2x cost.
        res = pipeline.process(option_proposal(legs, expected_edge_usd=1000.0),
                               make_account(), make_quote(), market_is_open=True)
        assert res.result.est_cost_usd >= 10.0  # used real leg spread, not the approximation


class TestLiveStageGate:
    def _live_pipeline(self, tmp_path, journal):
        config = make_config(mode="live")
        config.limits.live.approval_required = False  # isolate the stage gate
        return OrderPipeline(config, journal, OptionBroker(make_account()))

    def stock(self, **kw):
        d = dict(symbol="AAPL", side="buy", qty=5, order_type="limit", limit_price=100.0,
                 stop_price=95.0, expected_edge_usd=100.0, strategy_tag="t")
        d.update(kw)
        return OrderProposal(**d)

    def test_paper_stage_blocked_live(self, tmp_path):
        journal = Journal(tmp_path / "j.db")
        pipeline = self._live_pipeline(tmp_path, journal)
        # default stage is 'paper' -> not cleared for live
        res = pipeline.process(self.stock(), make_account(), make_quote(), market_is_open=True)
        assert res.status == "rejected"
        assert "strategy_stage" in {v.rule for v in res.result.violations}

    def test_small_live_stage_allowed(self, tmp_path):
        journal = Journal(tmp_path / "j.db")
        lifecycle.set_stage(journal, "t", "small-live")
        pipeline = self._live_pipeline(tmp_path, journal)
        res = pipeline.process(self.stock(), make_account(), make_quote(), market_is_open=True)
        assert res.status == "submitted"

    def test_small_live_scales_position_cap(self, tmp_path):
        journal = Journal(tmp_path / "j.db")
        lifecycle.set_stage(journal, "t", "small-live")  # 0.25 sizing fraction
        pipeline = self._live_pipeline(tmp_path, journal)
        # equity 50k, 10% cap = $5,000; small-live scales to $1,250.
        # A $2,000 order (20 sh @ $100) must now breach the scaled cap.
        res = pipeline.process(
            self.stock(qty=20, limit_price=100.0),
            make_account(equity=50000), make_quote(), market_is_open=True,
        )
        assert res.status == "rejected"
        assert "max_position" in {v.rule for v in res.result.violations}


class TestAsyncApproval:
    def test_live_option_queues_and_approves(self, tmp_path):
        config = make_config(mode="live")
        journal = Journal(tmp_path / "j.db")
        lifecycle.set_stage(journal, "test", "scaled")
        broker = OptionBroker(make_account())
        pipeline = OrderPipeline(config, journal, broker)
        legs = [
            OptionLeg(side="buy", right="call", strike=100, expiry=far(), qty=1, est_premium=5.0),
            OptionLeg(side="sell", right="call", strike=105, expiry=far(), qty=1, est_premium=3.0),
        ]
        res = pipeline.process(option_proposal(legs), make_account(), make_quote(),
                               market_is_open=True)
        assert res.status == "pending_approval"
        assert broker.option_orders == []  # nothing sent yet

        approved = pipeline.approve(res.proposal_id)
        assert approved.status == "submitted"
        assert len(broker.option_orders) == 1  # legs restored and submitted on approval
        assert len(broker.option_orders[0]["legs"]) == 2

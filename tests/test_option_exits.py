"""Option exits actually execute: the guardrail lets a validated close through the
defined-risk gate (a lone sell-to-close is not naked), a disguised naked short is
still caught, and the orchestrator submits the close instead of only flagging it."""

from datetime import date, timedelta

from conftest import make_config
from stubs import StubBroker, make_account

from trading.broker.models import AccountState, PositionView, Quote
from trading.broker.occ import build_occ
from trading.data.journal import Journal
from trading.guardrails.account_math import split_option_legs
from trading.guardrails.engine import GuardrailEngine
from trading.guardrails.models import OptionLeg, OrderProposal
from trading.orchestrator import CycleReport, Orchestrator


def _opt_pos(occ, qty, mv=500.0):
    return PositionView(symbol=occ, qty=qty, avg_entry_price=abs(mv) / (abs(qty) * 100),
                        market_value=mv, unrealized_pl=0.0, asset_class="option")


def _leg(side, right, strike, expiry, qty=1, occ=None):
    return OptionLeg(side=side, right=right, strike=strike, expiry=expiry, qty=qty,
                     est_premium=5.0, occ_symbol=occ)


# -- classifier ---------------------------------------------------------------

def test_split_recognizes_a_close_and_leaves_opens_alone():
    exp = date.today() + timedelta(days=30)
    occ = build_occ("AAPL", exp, "call", 190.0)
    held = [_opt_pos(occ, qty=2)]  # long 2 calls
    # sell 2 of the same OCC -> fully closing
    closing, opening = split_option_legs(
        [_leg("sell", "call", 190.0, exp, qty=2, occ=occ)], held, "AAPL")
    assert len(closing) == 1 and closing[0].qty == 2 and not opening
    # a naked short on a DIFFERENT strike we don't hold -> opening
    closing2, opening2 = split_option_legs(
        [_leg("sell", "call", 200.0, exp, qty=1)], held, "AAPL")
    assert not closing2 and len(opening2) == 1


def test_split_partial_close_splits_the_leg():
    exp = date.today() + timedelta(days=30)
    occ = build_occ("AAPL", exp, "put", 100.0)
    held = [_opt_pos(occ, qty=-1)]  # short 1 put
    # buy 3 -> 1 closes the short, 2 open new longs
    closing, opening = split_option_legs(
        [_leg("buy", "put", 100.0, exp, qty=3, occ=occ)], held, "AAPL")
    assert closing[0].qty == 1 and opening[0].qty == 2


# -- guardrail engine ---------------------------------------------------------

def _engine(tmp_path):
    return GuardrailEngine(make_config(), Journal(tmp_path / "j.db"))


def test_closing_a_long_call_near_expiry_is_approved(tmp_path):
    # A lone sell-to-close, 2 DTE (< min 7), would look naked and stale on an open —
    # but it offsets a held long, so it must pass.
    exp = date.today() + timedelta(days=2)
    occ = build_occ("AAPL", exp, "call", 190.0)
    account = AccountState(mode="paper", equity=100000, cash=100000, buying_power=200000,
                           last_equity=100000, daytrade_count=0, pattern_day_trader=False,
                           positions=[_opt_pos(occ, qty=1)])
    prop = OrderProposal(agent="exit_manager", strategy_tag="deterministic_exit",
                         symbol="AAPL", asset_class="option", side="sell",
                         legs=[_leg("sell", "call", 190.0, exp, occ=occ)],
                         reduces_position=True, expected_edge_usd=0.0)
    res = _engine(tmp_path).evaluate(prop, account, Quote(symbol="AAPL", bid=189, ask=191),
                                     market_is_open=True)
    assert res.approved, res.reasons


def test_disguised_naked_short_is_still_rejected(tmp_path):
    # Same shape but flagged reduces_position with NO offsetting holding -> naked.
    exp = date.today() + timedelta(days=30)
    account = AccountState(mode="paper", equity=100000, cash=100000, buying_power=200000,
                           last_equity=100000, daytrade_count=0, pattern_day_trader=False,
                           positions=[])
    prop = OrderProposal(agent="strategy", strategy_tag="sneaky", symbol="AAPL",
                         asset_class="option", side="sell",
                         legs=[_leg("sell", "call", 190.0, exp)],
                         reduces_position=True, expected_edge_usd=0.0)
    res = _engine(tmp_path).evaluate(prop, account, Quote(symbol="AAPL", bid=189, ask=191),
                                     market_is_open=True)
    assert not res.approved
    assert any(v.rule == "naked_option" for v in res.violations)


# -- orchestrator executes the close -----------------------------------------

class OptStubBroker(StubBroker):
    def get_option_quote(self, occ: str) -> Quote:
        return Quote(symbol=occ, bid=4.8, ask=5.2, last=5.0)

    def submit_option_order(self, *, legs, net_limit_price, underlying, client_order_id=None):
        self.option_orders = getattr(self, "option_orders", [])
        self.option_orders.append(dict(legs=legs, net_limit_price=net_limit_price,
                                       underlying=underlying))
        return f"stub-opt-{len(self.option_orders)}"


def test_orchestrator_submits_an_option_close_near_expiry(tmp_path):
    exp = date.today() + timedelta(days=2)  # inside the 7-DTE roll window
    occ = build_occ("AAPL", exp, "call", 190.0)
    account = make_account()
    account.positions = [_opt_pos(occ, qty=1)]
    broker = OptStubBroker(account, market_open=True)
    orch = Orchestrator(make_config(), Journal(tmp_path / "j.db"), broker, client=None)

    report = CycleReport(cycle="intraday")
    orch._manage_positions(account, report, market_open=True)

    assert getattr(broker, "option_orders", []), report.notes
    order = broker.option_orders[0]
    assert order["legs"][0]["side"] == "sell" and order["legs"][0]["qty"] == 1
    assert order["underlying"] == "AAPL"
    assert any("option exit" in n and "submitted" in n for n in report.notes)


if __name__ == "__main__":
    import sys
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))

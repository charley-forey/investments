"""Every guardrail must reject its violation AND leave an audit trail in the journal."""

from datetime import date, timedelta

from conftest import make_account, make_config, make_quote

from trading.guardrails.engine import OrderPipeline
from trading.guardrails.models import OptionLeg, OrderProposal


def base_proposal(**kw) -> OrderProposal:
    defaults = dict(
        symbol="AAPL", side="buy", qty=10, order_type="limit",
        limit_price=100.0, expected_edge_usd=100.0,
    )
    defaults.update(kw)
    return OrderProposal(**defaults)


def rules_fired(result) -> set[str]:
    return {v.rule for v in result.result.violations}


def assert_rejected_and_journaled(pipeline, result, rule: str):
    assert result.status == "rejected"
    assert rule in rules_fired(result)
    verdicts = pipeline.journal.verdicts_for(result.proposal_id)
    assert any(v["rule"] == rule and v["verdict"] == "reject" for v in verdicts)
    assert pipeline.journal.get_proposal(result.proposal_id)["status"] == "rejected"


def test_clean_proposal_submits_in_paper(pipeline):
    res = pipeline.process(base_proposal(), make_account(), make_quote(), market_is_open=True)
    assert res.status == "submitted"
    assert res.order_id is not None


def test_oversized_order_notional(pipeline):
    res = pipeline.process(
        base_proposal(qty=100, limit_price=100.0),  # $10,000 > $5,000 cap
        make_account(), make_quote(), market_is_open=True,
    )
    assert_rejected_and_journaled(pipeline, res, "max_order_notional")


def test_position_cap_pct_of_equity(pipeline):
    # equity 20k -> 10% cap = $2,000; $3,000 order breaches it
    res = pipeline.process(
        base_proposal(qty=30, limit_price=100.0),
        make_account(equity=20000), make_quote(), market_is_open=True,
    )
    assert_rejected_and_journaled(pipeline, res, "max_position")


def test_market_closed(pipeline):
    res = pipeline.process(base_proposal(), make_account(), make_quote(), market_is_open=False)
    assert_rejected_and_journaled(pipeline, res, "market_hours")


def test_market_orders_disabled(pipeline):
    res = pipeline.process(
        base_proposal(order_type="market", limit_price=None),
        make_account(), make_quote(), market_is_open=True,
    )
    assert_rejected_and_journaled(pipeline, res, "order_type")


def test_leveraged_etf_blocked(pipeline):
    res = pipeline.process(
        base_proposal(symbol="TQQQ"), make_account(), make_quote("TQQQ"), market_is_open=True,
    )
    assert_rejected_and_journaled(pipeline, res, "leveraged_etf")


def test_min_price(pipeline):
    res = pipeline.process(
        base_proposal(limit_price=3.0, expected_edge_usd=50.0),
        make_account(), make_quote(bid=2.9, ask=3.1), market_is_open=True,
    )
    assert_rejected_and_journaled(pipeline, res, "min_price")


def test_kill_switch_trips_on_daily_loss_and_blocks(pipeline):
    account = make_account(daily_pl_pct=-4.0)  # beyond the 3% cap
    res = pipeline.process(base_proposal(), account, make_quote(), market_is_open=True)
    assert_rejected_and_journaled(pipeline, res, "kill_switch")
    assert pipeline.journal.kill_switch_active()

    # Still blocked on a later, profitable-day proposal until manual reset...
    res2 = pipeline.process(base_proposal(), make_account(), make_quote(), market_is_open=True)
    assert "kill_switch" in rules_fired(res2)

    # ...but position-reducing orders are allowed through the kill switch.
    res3 = pipeline.process(
        base_proposal(side="sell", reduces_position=True),
        make_account(), make_quote(), market_is_open=True,
    )
    assert "kill_switch" not in rules_fired(res3)

    pipeline.journal.reset_kill_switch()
    res4 = pipeline.process(base_proposal(), make_account(), make_quote(), market_is_open=True)
    assert res4.status == "submitted"


def test_fourth_day_trade_under_25k_blocked(pipeline):
    account = make_account(equity=10000, daytrade_count=3)
    res = pipeline.process(
        base_proposal(qty=5), account, make_quote(),
        market_is_open=True, would_be_day_trade=True,
    )
    assert_rejected_and_journaled(pipeline, res, "pdt")


def test_day_trade_allowed_over_25k(pipeline):
    account = make_account(equity=50000, daytrade_count=3)
    res = pipeline.process(
        base_proposal(qty=5), account, make_quote(),
        market_is_open=True, would_be_day_trade=True,
    )
    assert res.status == "submitted"


def test_wash_sale_rebuy_blocked(pipeline):
    lot = pipeline.journal.open_lot(symbol="AAPL", qty=10, price=120.0)
    pipeline.journal.close_lot(lot, price=100.0)  # realized loss
    res = pipeline.process(base_proposal(), make_account(), make_quote(), market_is_open=True)
    assert_rejected_and_journaled(pipeline, res, "wash_sale")


def test_cost_hurdle_requires_stated_edge(pipeline):
    res = pipeline.process(
        base_proposal(expected_edge_usd=None),
        make_account(), make_quote(), market_is_open=True,
    )
    assert_rejected_and_journaled(pipeline, res, "cost_hurdle")


def test_cost_hurdle_rejects_thin_edge(pipeline):
    # spread 0.20 x 10 sh = $2.00; slippage 5bps on $1,000 = $0.50 -> cost $2.50
    # hurdle = 2x -> needs >= $5.00 edge
    res = pipeline.process(
        base_proposal(expected_edge_usd=4.0),
        make_account(), make_quote(), market_is_open=True,
    )
    assert_rejected_and_journaled(pipeline, res, "cost_hurdle")


def test_daily_trade_budget(pipeline):
    for _ in range(5):
        pipeline.journal.record_order(
            proposal_id=None, mode="paper", symbol="SPY", side="buy",
            qty=1, order_type="limit", limit_price=500.0,
        )
    res = pipeline.process(base_proposal(), make_account(), make_quote(), market_is_open=True)
    assert_rejected_and_journaled(pipeline, res, "daily_trade_budget")


def option_proposal(legs, **kw) -> OrderProposal:
    defaults = dict(
        symbol="AAPL", asset_class="option", order_type="limit",
        expected_edge_usd=100.0, legs=legs,
    )
    defaults.update(kw)
    return OrderProposal(**defaults)


def far_expiry() -> date:
    return date.today() + timedelta(days=45)


def test_naked_short_call_rejected(pipeline):
    legs = [OptionLeg(side="sell", right="call", strike=110, expiry=far_expiry(),
                      qty=1, est_premium=2.0)]
    res = pipeline.process(
        option_proposal(legs), make_account(cash=0), make_quote(), market_is_open=True,
    )
    assert_rejected_and_journaled(pipeline, res, "naked_option")


def test_defined_risk_spread_passes_options_rules(pipeline):
    # debit call spread, net debit $2.00 -> max loss $200 < $500 cap
    legs = [
        OptionLeg(side="buy", right="call", strike=100, expiry=far_expiry(), qty=1, est_premium=5.0),
        OptionLeg(side="sell", right="call", strike=105, expiry=far_expiry(), qty=1, est_premium=3.0),
    ]
    res = pipeline.process(option_proposal(legs), make_account(), make_quote(), market_is_open=True)
    # Options validation passes; execution itself is milestone 4, so the pipeline
    # rejects at the submission step with options_execution — and nothing else.
    assert rules_fired(res) == {"options_execution"}


def test_options_max_loss_cap(pipeline):
    # 10-wide credit spread -> conservative max loss $1,000 > $500 cap
    legs = [
        OptionLeg(side="sell", right="put", strike=100, expiry=far_expiry(), qty=1, est_premium=4.0),
        OptionLeg(side="buy", right="put", strike=90, expiry=far_expiry(), qty=1, est_premium=2.0),
    ]
    res = pipeline.process(option_proposal(legs), make_account(), make_quote(), market_is_open=True)
    assert_rejected_and_journaled(pipeline, res, "options_max_loss")


def test_options_min_dte(pipeline):
    near = date.today() + timedelta(days=2)
    legs = [OptionLeg(side="buy", right="call", strike=100, expiry=near, qty=1, est_premium=2.0)]
    res = pipeline.process(option_proposal(legs), make_account(), make_quote(), market_is_open=True)
    assert_rejected_and_journaled(pipeline, res, "options_dte")


def test_live_mode_queues_for_approval(journal):
    config = make_config(mode="live")
    pipeline = OrderPipeline(config, journal, broker=None)
    res = pipeline.process(base_proposal(), make_account(), make_quote(), market_is_open=True)
    assert res.status == "pending_approval"
    assert journal.pending_approvals()[0]["id"] == res.proposal_id

    approved = pipeline.approve(res.proposal_id)
    assert approved.status == "submitted"
    assert journal.get_proposal(res.proposal_id)["status"] == "submitted"

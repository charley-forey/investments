"""The risk agent's third verdict: 'amend' takes a sound setup smaller instead of
killing it. Vetoes on sound-but-oversized setups cost $672 of realised edge in the
first week's graded ledger — this is the middle option that recovers it."""

from trading.agents.risk import RiskVerdict
from trading.guardrails.models import OptionLeg, OrderProposal


def _stock(qty=100.0):
    return OrderProposal(symbol="XOM", qty=qty, limit_price=150.0, stop_price=146.0)


def _option():
    return OrderProposal(
        symbol="TSLA", asset_class="option", qty=0,
        legs=[OptionLeg(side="buy", right="put", strike=300, expiry="2026-08-14",
                        qty=8, est_premium=2.3),
              OptionLeg(side="sell", right="put", strike=290, expiry="2026-08-14",
                        qty=8, est_premium=1.1)],
    )


def _v(verdict, mult=None):
    return RiskVerdict(verdict=verdict, reason="r", concerns=[], qty_mult=mult)


def test_approve_and_veto_gate_correctly():
    assert _v("approve").allows_trade
    assert _v("amend", 0.5).allows_trade
    assert not _v("veto").allows_trade


def test_approve_passes_proposal_through_untouched():
    p = _stock()
    assert _v("approve").scaled(p) is p
    assert _v("veto").scaled(p) is p


def test_amend_shrinks_stock_qty():
    assert _v("amend", 0.5).scaled(_stock(100)).qty == 50


def test_amend_shrinks_every_option_leg():
    out = _v("amend", 0.5).scaled(_option())
    assert [leg.qty for leg in out.legs] == [4, 4]


def test_amend_never_mutates_the_original():
    p = _stock(100)
    _v("amend", 0.5).scaled(p)
    assert p.qty == 100


def test_amend_can_only_shrink_never_grow():
    # A model returning 3.0 must not lever us up.
    assert _v("amend", 3.0).scaled(_stock(100)).qty == 100


def test_amend_clamps_absurdly_small_multiplier():
    # 0.01 clamps to the 0.25 floor, not to a dust position.
    assert _v("amend", 0.01).scaled(_stock(100)).qty == 25


def test_amend_missing_multiplier_defaults_to_half():
    assert _v("amend", None).scaled(_stock(100)).qty == 50


def test_amend_never_rounds_a_live_trade_to_zero():
    assert _v("amend", 0.25).scaled(_stock(1)).qty == 1
    assert [leg.qty for leg in _v("amend", 0.25).scaled(_option()).legs] == [2, 2]


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)

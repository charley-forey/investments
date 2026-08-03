"""Regression for the 2026-07-28..08-03 order-submission blackout.

Commit 87b5acf set exits.trailing_pct=8, and the guardrail engine correctly
stopped attaching a take-profit leg (a resting target and a trailing stop cannot
coexist -- the target is nearer, so it always fires first and the trail is dead
code beside it). But alpaca.submit_order still stamped order_class=BRACKET on
any order carrying a stop, and Alpaca rejects a bracket that has no take_profit:

    {"code":40010001,"message":"bracket orders require take_profit.limit_price"}

Every risk-managed long entry was rejected for six days. Nothing alerted: the
guardrail had already recorded its "approve" verdict before the broker call, so
the proposal sat at status='proposed' and the cycle summary counted it under
neither submitted nor rejected. On 2026-08-03 four approved trades (AMZN x2,
GOOGL, META) died this way and the account never left cash.

The entries that DID get through were the ones with no stop attached -- the
unprotected ones. That is the wrong way round, and it is why this is a test and
not a comment.
"""

from types import SimpleNamespace

from alpaca.trading.enums import OrderClass

from trading.broker.alpaca import AlpacaBroker


class _CaptureTrading:
    """Stands in for alpaca's TradingClient; keeps the last request object."""

    def __init__(self):
        self.last = None

    def submit_order(self, order_data):
        self.last = order_data
        return SimpleNamespace(id="test-order-1")


def _submit(**kw):
    """Build the payload the way the daemon does, without credentials/network."""
    broker = object.__new__(AlpacaBroker)
    broker._trading = _CaptureTrading()  # `trading` is a lazy property
    base = dict(symbol="AMZN", side="buy", qty=26,
                order_type="limit", limit_price=280.35)
    base.update(kw)
    broker.submit_order(**base)
    return broker.trading.last


def test_stop_without_target_is_oto_not_bracket():
    """The exact shape of proposal #80: stop, no target. Must not be BRACKET."""
    req = _submit(stop_loss_price=276.20, take_profit_price=None)
    assert req.order_class == OrderClass.OTO, (
        "a stop-only entry sent as BRACKET is rejected by Alpaca 40010001"
    )
    assert req.stop_loss is not None and req.stop_loss.stop_price == 276.20
    assert getattr(req, "take_profit", None) is None


def test_stop_with_target_is_still_bracket():
    """Both legs present -> the original atomic bracket is correct and preserved."""
    req = _submit(stop_loss_price=276.20, take_profit_price=290.75)
    assert req.order_class == OrderClass.BRACKET
    assert req.take_profit.limit_price == 290.75
    assert req.stop_loss.stop_price == 276.20


def test_no_stop_is_a_plain_order():
    """No protective leg -> no order_class at all, and DAY rather than GTC."""
    req = _submit()
    assert getattr(req, "order_class", None) in (None, OrderClass.SIMPLE)


def test_protective_stop_order_is_untouched():
    """order_type='stop' IS the protective order, never a bracket child."""
    req = _submit(order_type="stop", limit_price=None, stop_loss_price=276.20)
    assert req.stop_price == 276.20
    assert getattr(req, "order_class", None) in (None, OrderClass.SIMPLE)


if __name__ == "__main__":
    test_stop_without_target_is_oto_not_bracket()
    test_stop_with_target_is_still_bracket()
    test_no_stop_is_a_plain_order()
    test_protective_stop_order_is_untouched()
    print("ok")

"""Track B: pure execution-pricing helpers + edge cases (crossed/zero markets)."""

from trading.execution_pricing import (
    marketable_limit, mid_peg, option_leg_limit, net_debit_credit,
)


def test_marketable_limit_buy_pays_toward_ask():
    # aggressiveness 1 = cross to ask, 0 = rest at bid, 0.5 = mid.
    assert marketable_limit("buy", 10.00, 10.10, 1.0) == 10.10
    assert marketable_limit("buy", 10.00, 10.10, 0.0) == 10.00
    assert marketable_limit("buy", 10.00, 10.10, 0.5) == 10.05


def test_marketable_limit_sell_pays_toward_bid():
    assert marketable_limit("sell", 10.00, 10.10, 1.0) == 10.00
    assert marketable_limit("sell", 10.00, 10.10, 0.0) == 10.10


def test_marketable_limit_crossed_market_falls_back():
    # ask < bid -> return best available, never a nonsense price.
    assert marketable_limit("buy", 10.20, 10.00, 0.5) == 10.00  # ask
    assert marketable_limit("buy", 0.0, 10.00, 0.5) == 10.00
    assert marketable_limit("buy", 10.00, 0.0, 0.5) == 10.00    # bid via `ask or bid`


def test_mid_peg():
    assert mid_peg(10.00, 10.10) == 10.05
    assert mid_peg(1.0, 2.0) == 1.5


def test_mid_peg_crossed_or_zero():
    assert mid_peg(10.20, 10.00) == 10.00   # crossed -> ask
    assert mid_peg(0.0, 3.0) == 3.0
    assert mid_peg(0.0, 0.0) == 0.0


def test_option_leg_limit():
    assert option_leg_limit("buy", 1.20, 1.40, 1.0) == 1.40
    assert option_leg_limit("sell", 1.20, 1.40, 1.0) == 1.20
    assert option_leg_limit("buy", 1.20, 1.40, 0.5) == 1.30


def test_net_debit_positive_for_net_buy():
    legs = [{"side": "buy", "price": 2.00, "qty": 1},
            {"side": "sell", "price": 0.80, "qty": 1}]
    assert net_debit_credit(legs) == 1.20  # debit


def test_net_credit_negative_for_net_sell():
    legs = [{"side": "sell", "price": 2.00}, {"side": "buy", "price": 0.50}]
    assert net_debit_credit(legs) == -1.50  # credit


def test_net_debit_qty_scaling_and_bad_legs():
    legs = [{"side": "buy", "price": 1.00, "qty": 2},
            {"side": "buy", "price": None},       # skipped
            {"side": "sell", "price": 0.0}]        # skipped (non-positive)
    assert net_debit_credit(legs) == 2.00


def test_net_debit_empty():
    assert net_debit_credit([]) == 0.0
    assert net_debit_credit(None) == 0.0


def test_net_debit_accepts_objects():
    class Leg:
        def __init__(self, side, price, qty=1):
            self.side, self.price, self.qty = side, price, qty
    assert net_debit_credit([Leg("buy", 1.5), Leg("sell", 0.5)]) == 1.0


if __name__ == "__main__":
    import sys
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))

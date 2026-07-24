"""Phase 4: credit verticals + systematic vol-premium structure selection."""

from __future__ import annotations

from datetime import date, timedelta

from trading.analytics.options import build_vertical
from trading.data.options_flow import get_flow_provider
from trading.scanner.vol_premium import suggest_vol_structure


# Build ContractRow list directly from strike->mid maps (no chain plumbing).
def _rows(spot=100.0):
    from trading.analytics.options import ContractRow
    exp = date.today() + timedelta(days=30)
    call_mid = {90: 11.0, 95: 7.0, 100: 3.5, 105: 1.5, 110: 0.6}
    put_mid = {90: 0.6, 95: 1.5, 100: 3.0, 105: 6.0, 110: 10.5}
    rows = []
    for strike, mid in call_mid.items():
        rows.append(ContractRow(occ=f"C{strike}", expiry=exp, right="call", strike=strike,
                                dte=30, moneyness=(strike - spot) / spot, bid=mid - 0.05,
                                ask=mid + 0.05, mid=mid, spread_bps=10, iv=0.3, delta=0.5,
                                gamma=0, theta=0, vega=0, last_size=1))
    for strike, mid in put_mid.items():
        rows.append(ContractRow(occ=f"P{strike}", expiry=exp, right="put", strike=strike,
                                dte=30, moneyness=(strike - spot) / spot, bid=mid - 0.05,
                                ask=mid + 0.05, mid=mid, spread_bps=10, iv=0.34, delta=-0.5,
                                gamma=0, theta=0, vega=0, last_size=1))
    return rows


class TestCreditVertical:
    def test_bullish_credit_is_a_bull_put_spread(self):
        # Sell the near-money put (100), buy the lower wing (95). Credit = 3.0 - 1.5.
        plan, note = build_vertical(_rows(), direction="bullish", spot=100.0,
                                    max_loss_usd=500.0, mode="credit")
        assert note == "ok" and plan.mode == "credit" and plan.right == "put"
        sell = next(l for l in plan.legs if l["side"] == "sell")
        buy = next(l for l in plan.legs if l["side"] == "buy")
        assert sell["strike"] == 100.0 and buy["strike"] == 95.0    # short near, long wing
        assert plan.net_premium == 1.5                               # credit received
        # Defined risk: max loss = (width - credit) * 100 * contracts.
        assert plan.max_loss_usd == (5.0 - 1.5) * 100 * plan.contracts
        assert plan.max_profit_usd == 1.5 * 100 * plan.contracts

    def test_bearish_credit_is_a_bear_call_spread(self):
        plan, note = build_vertical(_rows(), direction="bearish", spot=100.0,
                                    max_loss_usd=500.0, mode="credit")
        assert note == "ok" and plan.right == "call"
        sell = next(l for l in plan.legs if l["side"] == "sell")
        buy = next(l for l in plan.legs if l["side"] == "buy")
        assert sell["strike"] == 100.0 and buy["strike"] == 105.0   # short near, long higher wing

    def test_debit_still_works_and_net_debit_alias(self):
        plan, _ = build_vertical(_rows(), direction="bullish", spot=100.0,
                                 max_loss_usd=500.0, mode="debit")
        assert plan.mode == "debit" and plan.net_debit == plan.net_premium == 2.0


class TestSuggestVolStructure:
    def test_rich_iv_no_event_sells_premium_with_trend(self):
        assert suggest_vol_structure(80.0, "up", False) == ("credit", "bullish")
        assert suggest_vol_structure(80.0, "down", False) == ("credit", "bearish")

    def test_rich_iv_but_event_vetoes_the_credit(self):
        assert suggest_vol_structure(80.0, "up", True) is None   # don't sell into a binary

    def test_cheap_iv_with_catalyst_buys_premium(self):
        assert suggest_vol_structure(20.0, "up", True) == ("debit", "bullish")

    def test_sideways_or_missing_gives_no_lean(self):
        assert suggest_vol_structure(80.0, "sideways", False) is None
        assert suggest_vol_structure(None, "up", False) is None

    def test_mid_iv_does_nothing(self):
        assert suggest_vol_structure(50.0, "up", False) is None


class TestOptionsFlowSeamBlocked:
    def test_null_provider_returns_nothing(self):
        p = get_flow_provider(config=None)
        assert p.available is False and p.unusual_activity("AAPL") == []

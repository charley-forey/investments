"""Account math vs hand-computed examples: sizing, cost model, options risk,
after-tax expectancy."""

from datetime import date, timedelta

import pytest

from conftest import make_config, make_quote

from trading.guardrails import account_math as am
from trading.guardrails.models import OptionLeg, OrderProposal


def far() -> date:
    return date.today() + timedelta(days=45)


class TestStockSizing:
    def test_risk_based_share_count(self):
        # equity 10,000 @ 1% risk = $100 budget; entry 50, stop 48 -> $2/share -> 50 shares
        # position cap = min($5,000, 10% of 10,000 = $1,000) -> 1,000/50 = 20 shares wins
        shares = am.size_stock_position(
            equity=10_000, risk_per_trade_pct=1.0, entry_price=50.0, stop_price=48.0,
            max_position_usd=5_000, max_position_pct=10.0,
        )
        assert shares == 20

    def test_uncapped_when_position_limit_is_loose(self):
        shares = am.size_stock_position(
            equity=100_000, risk_per_trade_pct=1.0, entry_price=50.0, stop_price=48.0,
            max_position_usd=1_000_000, max_position_pct=100.0,
        )
        assert shares == 500  # 1,000 / 2

    def test_zero_when_no_stop_distance(self):
        assert am.size_stock_position(
            equity=10_000, risk_per_trade_pct=1.0, entry_price=50.0, stop_price=50.0,
            max_position_usd=5_000, max_position_pct=10.0,
        ) == 0


class TestCostModel:
    def test_stock_cost(self):
        config = make_config()
        proposal = OrderProposal(symbol="AAPL", qty=10, limit_price=100.0)
        quote = make_quote(bid=99.9, ask=100.1)  # $0.20 spread
        cost = am.estimate_cost_usd(proposal, quote, config.limits.cost_hurdle)
        # spread 0.20*10 = 2.00 ; slippage 5bps of 1,000 = 0.50
        assert cost == pytest.approx(2.50, abs=0.01)


class TestOptionsAnalysis:
    def test_long_call_risks_premium(self):
        legs = [OptionLeg(side="buy", right="call", strike=100, expiry=far(), qty=1, est_premium=2.5)]
        a = am.analyze_option_legs(legs)
        assert a.is_defined_risk
        assert a.max_loss_usd == pytest.approx(250.0)

    def test_debit_call_spread_net_debit(self):
        legs = [
            OptionLeg(side="buy", right="call", strike=100, expiry=far(), qty=1, est_premium=5.0),
            OptionLeg(side="sell", right="call", strike=105, expiry=far(), qty=1, est_premium=3.0),
        ]
        a = am.analyze_option_legs(legs)
        assert a.is_defined_risk
        assert a.max_loss_usd == pytest.approx(200.0)  # net debit 2.00 x 100

    def test_credit_call_spread_width(self):
        legs = [
            OptionLeg(side="sell", right="call", strike=100, expiry=far(), qty=1, est_premium=5.0),
            OptionLeg(side="buy", right="call", strike=105, expiry=far(), qty=1, est_premium=3.0),
        ]
        a = am.analyze_option_legs(legs)
        assert a.is_defined_risk
        assert a.max_loss_usd == pytest.approx(500.0)  # conservative: width, credit ignored

    def test_covered_call_needs_shares(self):
        legs = [OptionLeg(side="sell", right="call", strike=110, expiry=far(), qty=1, est_premium=2.0)]
        covered = am.analyze_option_legs(legs, underlying_shares_held=100)
        naked = am.analyze_option_legs(legs, underlying_shares_held=50)
        assert covered.is_defined_risk and covered.max_loss_usd == pytest.approx(0.0)
        assert not naked.is_defined_risk

    def test_cash_secured_put(self):
        legs = [OptionLeg(side="sell", right="put", strike=40, expiry=far(), qty=1, est_premium=1.0)]
        secured = am.analyze_option_legs(legs, cash_available=4_000)
        unsecured = am.analyze_option_legs(legs, cash_available=1_000)
        assert secured.is_defined_risk
        assert secured.max_loss_usd == pytest.approx(4_000.0)
        assert not unsecured.is_defined_risk

    def test_long_cannot_cover_shorter_dated_short(self):
        # long expires before the short -> not protective -> short is naked
        legs = [
            OptionLeg(side="buy", right="call", strike=100,
                      expiry=date.today() + timedelta(days=10), qty=1, est_premium=2.0),
            OptionLeg(side="sell", right="call", strike=105,
                      expiry=date.today() + timedelta(days=40), qty=1, est_premium=3.0),
        ]
        a = am.analyze_option_legs(legs)
        assert not a.is_defined_risk


class TestAfterTax:
    def test_short_vs_long_term_rates(self):
        rates = make_config().settings.tax  # ST 24%+5%=29%, LT 15%+5%=20%
        assert am.after_tax_pnl(100.0, "short", rates) == pytest.approx(71.0)
        assert am.after_tax_pnl(100.0, "long", rates) == pytest.approx(80.0)

    def test_expectancy(self):
        rates = make_config().settings.tax
        trades = [(100.0, "short"), (-50.0, "short")]
        # (71.0 + -35.5) / 2 = 17.75
        assert am.after_tax_expectancy(trades, rates) == pytest.approx(17.75)

    def test_empty(self):
        assert am.after_tax_expectancy([], make_config().settings.tax) == 0.0

"""M9: risk-based sizing, VaR/ES, correlation, drawdown circuit breaker,
regime-scaled exposure, correlation concentration."""

import pytest

from conftest import make_config, make_quote

from stubs import StubBroker, make_account

from trading.analytics import risk
from trading.broker.models import PositionView
from trading.data.journal import Journal
from trading.guardrails import account_math
from trading.guardrails.engine import GuardrailEngine, OrderPipeline
from trading.guardrails.models import OrderProposal


def stock(**kw):
    d = dict(symbol="AAPL", side="buy", qty=10, order_type="limit", limit_price=100.0,
             stop_price=95.0, expected_edge_usd=100.0, strategy_tag="t")
    d.update(kw)
    return OrderProposal(**d)


class TestSizing:
    def test_vol_target_size(self):
        # equity 100k, target 10% vol, asset 20% vol -> $50k position -> 1000 sh @ $50
        n = account_math.vol_target_size(
            equity=100_000, entry_price=50.0, annual_vol=0.20, target_annual_vol=0.10,
            max_position_usd=1e9, max_position_pct=100.0)
        assert n == 1000

    def test_vol_target_smaller_for_higher_vol(self):
        low = account_math.vol_target_size(
            equity=100_000, entry_price=50.0, annual_vol=0.15, target_annual_vol=0.10,
            max_position_usd=1e9, max_position_pct=100.0)
        high = account_math.vol_target_size(
            equity=100_000, entry_price=50.0, annual_vol=0.60, target_annual_vol=0.10,
            max_position_usd=1e9, max_position_pct=100.0)
        assert high < low

    def test_kelly_with_edge_capped(self):
        # W=0.6, R=2 -> f=0.4, capped at 0.25
        assert account_math.kelly_fraction(0.6, 2.0, cap=0.25) == pytest.approx(0.25)

    def test_kelly_no_edge_is_zero(self):
        assert account_math.kelly_fraction(0.4, 1.0) == 0.0

    def test_kelly_uncapped_value(self):
        assert account_math.kelly_fraction(0.6, 2.0, cap=1.0) == pytest.approx(0.4)


class TestRiskMetrics:
    def test_annualized_vol(self):
        closes = [100, 101, 100, 101, 100, 101]
        assert risk.annualized_vol(closes) > 0

    def test_var_and_es(self):
        returns = [-0.10, -0.05, -0.02, 0.0, 0.01, 0.02, 0.03, 0.04, 0.05, 0.06]
        var = risk.value_at_risk(returns, confidence=0.9)
        es = risk.expected_shortfall(returns, confidence=0.9)
        assert var > 0
        assert es >= var  # tail average is at least as bad as the VaR threshold

    def test_correlation_identical_and_inverse(self):
        a = [0.01, -0.02, 0.03, -0.01, 0.02]
        assert risk.correlation(a, a) == pytest.approx(1.0)
        assert risk.correlation(a, [-x for x in a]) == pytest.approx(-1.0)

    def test_max_correlation_to_holdings(self):
        cand = [100, 101, 102, 101, 103, 104]
        holdings = {
            "TWIN": [50, 50.5, 51, 50.5, 51.5, 52],   # moves together
            "FLAT": [10, 10, 10, 10, 10, 10],          # no variance
        }
        check = risk.max_correlation_to_holdings(cand, holdings)
        assert check.correlated_with == "TWIN"
        assert check.max_correlation > 0.9


class TestDrawdownCircuit:
    def test_peak_to_trough_trips_kill_switch(self, tmp_path):
        config = make_config()
        journal = Journal(tmp_path / "j.db")
        pipeline = OrderPipeline(config, journal, StubBroker(make_account()))
        # First cycle at a high equity establishes the peak.
        pipeline.process(stock(), make_account(equity=100_000), make_quote(),
                         market_is_open=True)
        assert not journal.kill_switch_active()
        # Equity down 20% from peak -> breaches the 15% circuit breaker.
        res = pipeline.process(stock(), make_account(equity=80_000), make_quote(),
                               market_is_open=True)
        assert journal.kill_switch_active()
        assert res.status == "rejected"


class TestRegimeAndCorrelationGates:
    def test_regime_scale_tightens_gross_cap(self, tmp_path):
        config = make_config()
        journal = Journal(tmp_path / "j.db")
        engine = GuardrailEngine(config, journal)
        # equity 50k, gross cap 150% = 75k normally. Existing 60k + 2k order = 62k
        # passes normally, but a 0.5 regime scale drops the cap to 37.5k -> breach.
        account = make_account(equity=50_000, positions=[
            PositionView(symbol="MSFT", qty=1, avg_entry_price=60000.0,
                         market_value=60000.0, unrealized_pl=0.0)])
        res = engine.evaluate(stock(qty=20, limit_price=100.0), account, make_quote(),
                              market_is_open=True, regime_gross_scale=0.5)
        assert "max_gross_exposure" in {v.rule for v in res.violations}

    def test_correlation_concentration_blocks(self, tmp_path):
        config = make_config()
        journal = Journal(tmp_path / "j.db")
        engine = GuardrailEngine(config, journal)
        account = make_account(equity=200_000, positions=[
            PositionView(symbol="MSFT", qty=10, avg_entry_price=100.0,
                         market_value=1000.0, unrealized_pl=0.0)])
        res = engine.evaluate(stock(), account, make_quote(),
                              market_is_open=True, max_holding_correlation=0.97)
        assert "correlation_concentration" in {v.rule for v in res.violations}

    def test_low_correlation_allowed(self, tmp_path):
        config = make_config()
        journal = Journal(tmp_path / "j.db")
        engine = GuardrailEngine(config, journal)
        account = make_account(equity=200_000)
        res = engine.evaluate(stock(), account, make_quote(),
                              market_is_open=True, max_holding_correlation=0.3)
        assert "correlation_concentration" not in {v.rule for v in res.violations}

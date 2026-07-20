"""Deferred-then-wired risk layers: volatility-targeted sizing (orchestrator) and the
net directional-delta guardrail (engine)."""

from conftest import make_account, make_config
from stubs import StubBroker

from trading.broker.models import PositionView, Quote
from trading.config import PortfolioLimits
from trading.data.journal import Journal
from trading.guardrails.engine import GuardrailEngine
from trading.guardrails.models import OrderProposal
from trading.orchestrator import CycleReport, Orchestrator


def _q(sym="MSFT"):
    return Quote(symbol=sym, bid=99.9, ask=100.1)


def _stock(symbol, qty, mv):
    return PositionView(symbol=symbol, qty=qty, avg_entry_price=100.0,
                        market_value=mv, unrealized_pl=0.0, asset_class="stock")


class TestNetDeltaGuardrail:
    def test_rejects_overexposed_book(self, tmp_path):
        config = make_config(portfolio=PortfolioLimits(max_net_delta_pct=100.0))
        eng = GuardrailEngine(config, Journal(tmp_path / "j.db"))
        account = make_account(equity=100000.0, positions=[_stock("AAPL", 980, 98000.0)])
        proposal = OrderProposal(symbol="MSFT", side="buy", qty=40, order_type="limit",
                                 limit_price=100.0, expected_edge_usd=100.0)
        res = eng.evaluate(proposal, account, _q(), market_is_open=True)
        # 98k existing + 4k new = 102k net delta > 100k cap
        assert "max_net_delta" in {v.rule for v in res.violations}

    def test_allows_within_cap(self, tmp_path):
        config = make_config(portfolio=PortfolioLimits(max_net_delta_pct=100.0))
        eng = GuardrailEngine(config, Journal(tmp_path / "j.db"))
        account = make_account(equity=100000.0, positions=[_stock("AAPL", 400, 40000.0)])
        proposal = OrderProposal(symbol="MSFT", side="buy", qty=40, order_type="limit",
                                 limit_price=100.0, expected_edge_usd=100.0)
        res = eng.evaluate(proposal, account, _q(), market_is_open=True)
        assert "max_net_delta" not in {v.rule for v in res.violations}  # 44k < 100k

    def test_off_by_default(self, tmp_path):
        # default max_net_delta_pct is 0 -> the check never fires
        eng = GuardrailEngine(make_config(), Journal(tmp_path / "j.db"))
        account = make_account(equity=1000.0, positions=[_stock("AAPL", 9800, 980000.0)])
        proposal = OrderProposal(symbol="MSFT", side="buy", qty=40, order_type="limit",
                                 limit_price=100.0, expected_edge_usd=100.0)
        res = eng.evaluate(proposal, account, _q(), market_is_open=True)
        assert "max_net_delta" not in {v.rule for v in res.violations}


class TestVolSizing:
    def _orch(self, tmp_path, vol):
        config = make_config(portfolio=PortfolioLimits(vol_target_annual=0.15))
        journal = Journal(tmp_path / "j.db")
        if vol is not None:
            journal.record_snapshot(cycle="intraday", symbol="NVDA", bid=100, ask=100,
                                    last=100, spread_bps=1.0,
                                    features={"realized_vol": vol}, sentiment=None,
                                    mention_count=None)
        broker = StubBroker(make_account(equity=100000.0))
        return Orchestrator(config, journal, broker, client=None), journal

    def test_shrinks_oversized_high_vol_entry(self, tmp_path):
        orch, _ = self._orch(tmp_path, vol=0.60)  # high vol -> small target
        draft = OrderProposal(symbol="NVDA", side="buy", qty=1000, order_type="limit",
                              limit_price=100.0, expected_edge_usd=100.0, strategy_tag="x")
        report = CycleReport(cycle="intraday")
        orch._risk_size(draft, make_account(equity=100000.0), report)
        # weight=min(0.15/0.60,0.10)=0.10 -> 0.10*100k/100 = 100 shares
        assert draft.qty == 100
        assert any("vol-sized" in n for n in report.notes)

    def test_never_grows_a_small_entry(self, tmp_path):
        orch, _ = self._orch(tmp_path, vol=0.20)
        draft = OrderProposal(symbol="NVDA", side="buy", qty=5, order_type="limit",
                              limit_price=100.0, expected_edge_usd=100.0, strategy_tag="x")
        report = CycleReport(cycle="intraday")
        orch._risk_size(draft, make_account(equity=100000.0), report)
        assert draft.qty == 5  # already small -> untouched (only shrinks)

    def test_no_vol_leaves_qty(self, tmp_path):
        orch, _ = self._orch(tmp_path, vol=None)  # no snapshot -> no vol
        draft = OrderProposal(symbol="NVDA", side="buy", qty=1000, order_type="limit",
                              limit_price=100.0, expected_edge_usd=100.0, strategy_tag="x")
        report = CycleReport(cycle="intraday")
        orch._risk_size(draft, make_account(equity=100000.0), report)
        assert draft.qty == 1000  # best-effort: no vol data -> unchanged

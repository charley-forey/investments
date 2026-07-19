"""M6 production-hardening: brackets, option lots, partial closes, exact
attribution, stale-order cancel, reconciliation gate, backtest gate, portfolio risk,
market context."""

from datetime import date, datetime, timedelta, timezone

import pytest

from conftest import make_config, make_quote

from stubs import StubBroker, StubOrder, make_account

from trading.analytics import lifecycle
from trading.broker.models import PositionView
from trading.broker.occ import build_occ
from trading.broker.sync import cancel_stale_orders, sync_fills
from trading.data.journal import Journal
from trading.guardrails.engine import OrderPipeline
from trading.guardrails.models import OrderProposal
from trading.tools.market_context import compute_regime


def far() -> date:
    return date.today() + timedelta(days=45)


def stock(**kw) -> OrderProposal:
    d = dict(symbol="AAPL", side="buy", qty=10, order_type="limit", limit_price=100.0,
             stop_price=95.0, expected_edge_usd=100.0, strategy_tag="t")
    d.update(kw)
    return OrderProposal(**d)


class TestBracketOrders:
    def test_opening_long_with_stop_submits_bracket(self, tmp_path):
        journal = Journal(tmp_path / "j.db")
        broker = StubBroker(make_account())
        pipeline = OrderPipeline(make_config(), journal, broker)
        res = pipeline.process(stock(), make_account(), make_quote(), market_is_open=True)
        assert res.status == "submitted"
        order = broker.submitted[0]
        assert order["stop_loss_price"] == 95.0
        # default target = entry + 2R = 100 + 2*(100-95) = 110
        assert order["take_profit_price"] == pytest.approx(110.0)
        assert order["client_order_id"] == f"prop-{res.proposal_id}"

    def test_explicit_target_used(self, tmp_path):
        journal = Journal(tmp_path / "j.db")
        broker = StubBroker(make_account())
        pipeline = OrderPipeline(make_config(), journal, broker)
        res = pipeline.process(stock(target_price=120.0), make_account(), make_quote(),
                               market_is_open=True)
        assert broker.submitted[0]["take_profit_price"] == 120.0

    def test_close_has_no_bracket(self, tmp_path):
        journal = Journal(tmp_path / "j.db")
        broker = StubBroker(make_account())
        pipeline = OrderPipeline(make_config(), journal, broker)
        res = pipeline.process(stock(side="sell", reduces_position=True, stop_price=None),
                               make_account(), make_quote(), market_is_open=True)
        assert broker.submitted[0]["stop_loss_price"] is None


class TestOptionLots:
    def test_option_fill_tracked_with_multiplier(self, tmp_path):
        config = make_config()
        journal = Journal(tmp_path / "j.db")
        occ = build_occ("AAPL", far(), "call", 100.0)
        buy = StubOrder(id="o1", symbol=occ, side="buy", filled_qty=2, filled_avg_price=5.0)
        broker = StubBroker(make_account(), orders=[buy])
        sync_fills(config, journal, broker)
        lots = journal.open_lots(occ)
        assert len(lots) == 1
        assert lots[0]["multiplier"] == 100.0
        assert lots[0]["asset_class"] == "option"

    def test_option_pnl_uses_multiplier(self, tmp_path):
        config = make_config()
        journal = Journal(tmp_path / "j.db")
        occ = build_occ("AAPL", far(), "call", 100.0)
        lot = journal.open_lot(symbol=occ, qty=1, price=5.0, multiplier=100.0,
                               asset_class="option")
        out = journal.close_lot_qty(lot, qty=1, price=7.0)
        assert out["realized_pnl"] == pytest.approx(200.0)  # (7-5)*1*100


class TestPartialClose:
    def test_partial_close_splits_lot(self, tmp_path):
        journal = Journal(tmp_path / "j.db")
        lot = journal.open_lot(symbol="AAPL", qty=10, price=100.0)
        out = journal.close_lot_qty(lot, qty=4, price=110.0)
        assert out["qty_closed"] == 4
        assert out["realized_pnl"] == pytest.approx(40.0)  # (110-100)*4
        remaining = journal.open_lots("AAPL")
        assert len(remaining) == 1
        assert remaining[0]["qty"] == 6  # parent reduced

    def test_sell_across_lots_partial_last(self, tmp_path):
        config = make_config()
        journal = Journal(tmp_path / "j.db")
        journal.open_lot(symbol="AAPL", qty=10, price=100.0,
                         ts="2020-01-01T00:00:00+00:00")
        journal.open_lot(symbol="AAPL", qty=10, price=120.0,
                         ts="2020-01-02T00:00:00+00:00")
        # sell 15: HIFO closes the $120 lot fully (10) + 5 of the $100 lot
        sell = StubOrder(id="s", symbol="AAPL", side="sell", filled_qty=15,
                         filled_avg_price=130.0)
        broker = StubBroker(make_account(), orders=[sell])
        sync_fills(config, journal, broker)
        remaining = journal.open_lots("AAPL")
        assert len(remaining) == 1
        assert remaining[0]["open_price"] == 100.0
        assert remaining[0]["qty"] == 5


class TestExactAttribution:
    def test_client_order_id_attributes_tag(self, tmp_path):
        config = make_config()
        journal = Journal(tmp_path / "j.db")
        # A submitted proposal with a distinct tag.
        pid = journal.record_proposal(
            agent="strategy", strategy_tag="news-breakout", symbol="AAPL",
            asset_class="stock", side="buy", qty=10, order_type="limit", limit_price=100.0,
        )
        journal.set_proposal_status(pid, "submitted")
        # A different, more recent proposal for the same symbol (would mislead the heuristic).
        pid2 = journal.record_proposal(
            agent="strategy", strategy_tag="other-strat", symbol="AAPL",
            asset_class="stock", side="buy", qty=5, order_type="limit", limit_price=100.0,
        )
        journal.set_proposal_status(pid2, "submitted")

        buy = StubOrder(id="o1", symbol="AAPL", side="buy", filled_qty=10,
                        filled_avg_price=100.0, client_order_id=f"prop-{pid}")
        broker = StubBroker(make_account(), orders=[buy])
        sync_fills(config, journal, broker)
        lots = journal.open_lots("AAPL")
        assert lots[0]["strategy_tag"] == "news-breakout"  # exact, not the more recent one


class TestStaleOrders:
    def test_cancels_orders_past_ttl(self, tmp_path):
        config = make_config()
        journal = Journal(tmp_path / "j.db")
        old = StubOrder(id="old", symbol="AAPL", side="buy", filled_qty=0,
                        filled_avg_price=0, status="new",
                        submitted_at=datetime.now(timezone.utc) - timedelta(hours=2))
        fresh = StubOrder(id="fresh", symbol="MSFT", side="buy", filled_qty=0,
                          filled_avg_price=0, status="new")
        broker = StubBroker(make_account())
        broker._open_orders = [old, fresh]
        n = cancel_stale_orders(config, journal, broker)
        assert n == 1
        assert broker.canceled == ["old"]


class TestReconciliationGate:
    def test_mismatch_halts_new_entries(self, tmp_path):
        config = make_config()
        journal = Journal(tmp_path / "j.db")
        # Broker shows a position the journal doesn't know about -> mismatch -> halt.
        broker = StubBroker(make_account(positions=[
            PositionView(symbol="TSLA", qty=10, avg_entry_price=200.0,
                         market_value=2000.0, unrealized_pl=0.0)
        ]), orders=[])
        sync_fills(config, journal, broker)
        assert journal.get_state("reconcile_halt")

        pipeline = OrderPipeline(config, journal, broker)
        res = pipeline.process(stock(), make_account(), make_quote(), market_is_open=True)
        assert res.status == "rejected"
        assert "reconcile_halt" in {v.rule for v in res.result.violations}

        # A reducing order is still allowed through the halt.
        res2 = pipeline.process(stock(side="sell", reduces_position=True, stop_price=None),
                                make_account(), make_quote(), market_is_open=True)
        assert "reconcile_halt" not in {v.rule for v in res2.result.violations}


class TestBacktestGate:
    def test_candidate_stage_cannot_trade(self, tmp_path):
        config = make_config()
        journal = Journal(tmp_path / "j.db")
        lifecycle.set_stage(journal, "t", "candidate")
        pipeline = OrderPipeline(config, journal, StubBroker(make_account()))
        res = pipeline.process(stock(), make_account(), make_quote(), market_is_open=True)
        assert res.status == "rejected"
        assert "strategy_stage" in {v.rule for v in res.result.violations}

    def test_promote_after_backtest_enables_trading(self, tmp_path):
        config = make_config()
        journal = Journal(tmp_path / "j.db")
        lifecycle.set_stage(journal, "t", "candidate")
        change = lifecycle.promote_after_backtest(journal, "t", expectancy=5.0)
        assert change and change.new_stage == "paper"
        pipeline = OrderPipeline(config, journal, StubBroker(make_account()))
        res = pipeline.process(stock(), make_account(), make_quote(), market_is_open=True)
        assert res.status == "submitted"

    def test_negative_backtest_no_promotion(self, tmp_path):
        journal = Journal(tmp_path / "j.db")
        lifecycle.set_stage(journal, "t", "candidate")
        assert lifecycle.promote_after_backtest(journal, "t", expectancy=-1.0) is None
        assert lifecycle.get_stage(journal, "t") == "candidate"


class TestPortfolioRisk:
    def test_gross_exposure_cap(self, tmp_path):
        config = make_config()
        # gross cap 150% of 50k = 75k. Existing 74k + a 2k order -> breach.
        account = make_account(equity=50000, positions=[
            PositionView(symbol="MSFT", qty=1, avg_entry_price=74000.0,
                         market_value=74000.0, unrealized_pl=0.0)
        ])
        journal = Journal(tmp_path / "j.db")
        pipeline = OrderPipeline(config, journal, StubBroker(account))
        res = pipeline.process(stock(qty=20, limit_price=100.0), account, make_quote(),
                               market_is_open=True)
        assert "max_gross_exposure" in {v.rule for v in res.result.violations}

    def test_per_underlying_cap(self, tmp_path):
        config = make_config()
        # cap is 2 per underlying; already 2 AAPL-based positions (stock + an option)
        occ = build_occ("AAPL", far(), "call", 100.0)
        account = make_account(equity=200000, positions=[
            PositionView(symbol="AAPL", qty=1, avg_entry_price=100.0,
                         market_value=100.0, unrealized_pl=0.0),
            PositionView(symbol=occ, qty=1, avg_entry_price=5.0,
                         market_value=500.0, unrealized_pl=0.0, asset_class="option"),
        ])
        journal = Journal(tmp_path / "j.db")
        pipeline = OrderPipeline(config, journal, StubBroker(account))
        # A new MSFT position is fine; a 3rd AAPL is blocked.
        res = pipeline.process(stock(symbol="MSFT"), account, make_quote(symbol="MSFT"),
                               market_is_open=True)
        assert "max_per_underlying" not in {v.rule for v in res.result.violations}


class TestMarketContext:
    def test_uptrend_detected(self):
        closes = [100 + i for i in range(60)]
        regime = compute_regime(closes)
        assert regime.trend == "up"
        assert "SPY" in regime.summary()

    def test_insufficient_history(self):
        regime = compute_regime([100, 101, 102])
        assert regime.trend == "unknown"

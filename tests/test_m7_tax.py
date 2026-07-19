"""M7: wash-sale cost-basis deferral, realized-gains reporting/export, harvesting."""

import pytest

from conftest import make_config, make_quote

from stubs import StubBroker, make_account

from trading.analytics.tax import (
    apply_wash_sale_adjustments, export_realized_gains_csv, harvest_candidates,
    realized_gains_report, realized_totals,
)
from trading.broker.models import PositionView
from trading.data.journal import Journal
from trading.guardrails.engine import OrderPipeline
from trading.guardrails.models import OrderProposal


def seed_loss(journal, symbol="AAPL", qty=10, open_price=100.0, close_price=90.0,
              open_ts="2026-01-01T00:00:00+00:00", close_ts="2026-02-01T00:00:00+00:00"):
    lot = journal.open_lot(symbol=symbol, qty=qty, price=open_price, ts=open_ts)
    journal.close_lot(lot, price=close_price, ts=close_ts)
    return lot


class TestWashSale:
    def test_full_deferral(self, tmp_path):
        journal = Journal(tmp_path / "j.db")
        loss = seed_loss(journal)  # -100 loss
        repl = journal.open_lot(symbol="AAPL", qty=10, price=95.0,
                                ts="2026-02-10T00:00:00+00:00")

        adj = apply_wash_sale_adjustments(journal, window_days=30)
        assert len(adj) == 1
        assert adj[0].disallowed_usd == pytest.approx(100.0)

        repl_lot = next(l for l in journal.open_lots("AAPL") if l["id"] == repl)
        assert repl_lot["open_price"] == pytest.approx(105.0)  # 95 + 100/10
        assert repl_lot["wash_adjusted"] == 1
        # holding period extended: open_ts backdated by the loss lot's 31 days
        assert repl_lot["open_ts"] < "2026-02-10T00:00:00+00:00"

        loss_lot = journal.conn.execute(
            "SELECT * FROM tax_lots WHERE id=?", (loss,)).fetchone()
        assert loss_lot["wash_sale_flag"] == 1
        assert loss_lot["wash_disallowed"] == pytest.approx(100.0)

    def test_partial_deferral(self, tmp_path):
        journal = Journal(tmp_path / "j.db")
        seed_loss(journal)  # -100 over 10 shares
        journal.open_lot(symbol="AAPL", qty=4, price=95.0,
                         ts="2026-02-10T00:00:00+00:00")  # only 4 replacement shares
        adj = apply_wash_sale_adjustments(journal, window_days=30)
        assert adj[0].disallowed_usd == pytest.approx(40.0)  # 100 * 4/10

    def test_no_adjustment_outside_window(self, tmp_path):
        journal = Journal(tmp_path / "j.db")
        seed_loss(journal)
        journal.open_lot(symbol="AAPL", qty=10, price=95.0,
                         ts="2026-04-01T00:00:00+00:00")  # >30 days after close
        assert apply_wash_sale_adjustments(journal, window_days=30) == []

    def test_idempotent(self, tmp_path):
        journal = Journal(tmp_path / "j.db")
        seed_loss(journal)
        journal.open_lot(symbol="AAPL", qty=10, price=95.0,
                         ts="2026-02-10T00:00:00+00:00")
        assert len(apply_wash_sale_adjustments(journal, 30)) == 1
        assert apply_wash_sale_adjustments(journal, 30) == []  # no re-processing

    def test_gain_not_flagged(self, tmp_path):
        journal = Journal(tmp_path / "j.db")
        lot = journal.open_lot(symbol="AAPL", qty=10, price=100.0,
                               ts="2026-01-01T00:00:00+00:00")
        journal.close_lot(lot, price=110.0, ts="2026-02-01T00:00:00+00:00")  # a gain
        journal.open_lot(symbol="AAPL", qty=10, price=105.0,
                         ts="2026-02-10T00:00:00+00:00")
        assert apply_wash_sale_adjustments(journal, 30) == []  # gains aren't wash sales


class TestRealizedGains:
    def test_report_allowed_pnl_reflects_wash(self, tmp_path):
        journal = Journal(tmp_path / "j.db")
        loss = seed_loss(journal)  # -100
        journal.open_lot(symbol="AAPL", qty=10, price=95.0,
                         ts="2026-02-10T00:00:00+00:00")
        apply_wash_sale_adjustments(journal, 30)

        rows = realized_gains_report(journal)
        loss_row = next(r for r in rows if r.realized_pnl < 0)
        assert loss_row.realized_pnl == pytest.approx(-100.0)
        assert loss_row.wash_disallowed == pytest.approx(100.0)
        assert loss_row.allowed_pnl == pytest.approx(0.0)  # loss deferred, not taxable now

    def test_totals_split_short_long(self, tmp_path):
        journal = Journal(tmp_path / "j.db")
        # short-term gain
        l1 = journal.open_lot(symbol="A", qty=10, price=100.0, ts="2026-01-01T00:00:00+00:00")
        journal.close_lot(l1, price=110.0, ts="2026-02-01T00:00:00+00:00")
        # long-term gain (>365 days)
        l2 = journal.open_lot(symbol="B", qty=10, price=100.0, ts="2024-01-01T00:00:00+00:00")
        journal.close_lot(l2, price=120.0, ts="2026-01-01T00:00:00+00:00")

        totals = realized_totals(realized_gains_report(journal))
        assert totals["short_term"] == pytest.approx(100.0)
        assert totals["long_term"] == pytest.approx(200.0)
        assert totals["total"] == pytest.approx(300.0)

    def test_year_filter(self, tmp_path):
        journal = Journal(tmp_path / "j.db")
        l = journal.open_lot(symbol="A", qty=1, price=100.0, ts="2025-01-01T00:00:00+00:00")
        journal.close_lot(l, price=110.0, ts="2025-06-01T00:00:00+00:00")
        assert len(realized_gains_report(journal, year=2025)) == 1
        assert len(realized_gains_report(journal, year=2026)) == 0

    def test_export_csv(self, tmp_path):
        journal = Journal(tmp_path / "j.db")
        l = journal.open_lot(symbol="A", qty=1, price=100.0, ts="2026-01-01T00:00:00+00:00")
        journal.close_lot(l, price=110.0, ts="2026-02-01T00:00:00+00:00")
        out = tmp_path / "gains.csv"
        n = export_realized_gains_csv(journal, str(out))
        assert n == 1
        text = out.read_text(encoding="utf-8")
        assert "allowed_pnl" in text and "A" in text


class TestHarvest:
    def test_candidates_are_losers(self):
        account = make_account(positions=[
            PositionView(symbol="NVDA", qty=10, avg_entry_price=200.0,
                         market_value=1500.0, unrealized_pl=-500.0),
            PositionView(symbol="AAPL", qty=10, avg_entry_price=100.0,
                         market_value=1200.0, unrealized_pl=200.0),
            PositionView(symbol="F", qty=10, avg_entry_price=12.0,
                         market_value=115.0, unrealized_pl=-5.0),  # below threshold
        ])
        cands = harvest_candidates(account, min_loss_usd=100.0)
        assert [c.symbol for c in cands] == ["NVDA"]  # only the >$100 loser


class TestWashSaleMode:
    def _pipeline(self, tmp_path, mode):
        from trading.config import WashSaleLimits

        config = make_config()
        config.limits.wash_sale = WashSaleLimits(enforce=True, window_days=30, mode=mode)
        journal = Journal(tmp_path / "j.db")
        lot = journal.open_lot(symbol="AAPL", qty=10, price=120.0)
        journal.close_lot(lot, price=100.0)  # realized loss
        return OrderPipeline(config, journal, StubBroker(make_account())), journal

    def proposal(self):
        return OrderProposal(symbol="AAPL", side="buy", qty=5, order_type="limit",
                             limit_price=100.0, stop_price=95.0, expected_edge_usd=100.0,
                             strategy_tag="t")

    def test_block_mode_rejects_rebuy(self, tmp_path):
        pipeline, _ = self._pipeline(tmp_path, "block")
        res = pipeline.process(self.proposal(), make_account(), make_quote(),
                               market_is_open=True)
        assert "wash_sale" in {v.rule for v in res.result.violations}

    def test_defer_mode_allows_rebuy(self, tmp_path):
        pipeline, _ = self._pipeline(tmp_path, "defer")
        res = pipeline.process(self.proposal(), make_account(), make_quote(),
                               market_is_open=True)
        assert "wash_sale" not in {v.rule for v in res.result.violations}
        assert res.status == "submitted"

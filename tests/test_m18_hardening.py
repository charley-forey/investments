"""M18: partial-fill sync, terminal statuses, cost cap, preflight, notifications."""

from conftest import make_config, make_quote

from stubs import StubBroker, StubOrder, make_account

from trading.broker.sync import sync_fills
from trading.data.journal import Journal


class TestPartialFills:
    def test_incremental_delta_recording(self, tmp_path):
        config = make_config()
        journal = Journal(tmp_path / "j.db")
        order = StubOrder(id="o1", symbol="AAPL", side="buy", filled_qty=5,
                          filled_avg_price=100.0, status="partially_filled")
        broker = StubBroker(make_account(), orders=[order])

        r1 = sync_fills(config, journal, broker)
        assert r1.fills_recorded == 1
        lots = journal.open_lots("AAPL")
        assert sum(l["qty"] for l in lots) == 5

        # Order completes: now 10 filled. Only the incremental 5 should be recorded.
        order.filled_qty = 10
        order.status = "filled"
        r2 = sync_fills(config, journal, broker)
        assert r2.fills_recorded == 1  # incremental, not the full 10 again
        lots = journal.open_lots("AAPL")
        assert sum(l["qty"] for l in lots) == 10  # 5 + 5, no double-count/loss

        # Nothing new on a third sync.
        assert sync_fills(config, journal, broker).fills_recorded == 0

    def test_partial_sell_closes_incrementally(self, tmp_path):
        config = make_config()
        journal = Journal(tmp_path / "j.db")
        journal.open_lot(symbol="AAPL", qty=10, price=100.0)
        order = StubOrder(id="s1", symbol="AAPL", side="sell", filled_qty=4,
                          filled_avg_price=110.0, status="partially_filled")
        broker = StubBroker(make_account(), orders=[order])
        sync_fills(config, journal, broker)
        assert sum(l["qty"] for l in journal.open_lots("AAPL")) == 6  # 4 sold
        order.filled_qty = 10
        order.status = "filled"
        sync_fills(config, journal, broker)
        assert journal.open_lots("AAPL") == []  # remaining 6 sold


class TestTerminalStatuses:
    def test_canceled_records_status_no_fill(self, tmp_path):
        config = make_config()
        journal = Journal(tmp_path / "j.db")
        order = StubOrder(id="c1", symbol="AAPL", side="buy", filled_qty=0,
                          filled_avg_price=0.0, status="canceled")
        r = sync_fills(config, journal, StubBroker(make_account(), orders=[order]))
        assert r.fills_recorded == 0
        assert journal.open_lots("AAPL") == []
        assert journal.get_state("order_status:c1") == "canceled"

    def test_rejected_records_status(self, tmp_path):
        config = make_config()
        journal = Journal(tmp_path / "j.db")
        order = StubOrder(id="r1", symbol="AAPL", side="buy", filled_qty=0,
                          filled_avg_price=0.0, status="rejected")
        sync_fills(config, journal, StubBroker(make_account(), orders=[order]))
        assert journal.get_state("order_status:r1") == "rejected"


class TestCostCap:
    def _orch(self, tmp_path, cap, seeded_cost):
        from stubs import make_account as acct
        from trading.agents.runner import AgentResult
        from trading.orchestrator import Orchestrator
        config = make_config()
        config.settings.agents.max_daily_cost_usd = cap
        journal = Journal(tmp_path / "j.db")
        if seeded_cost:
            journal.record_usage(cycle="intraday", agent="strategy", model="m",
                                 input_tokens=0, output_tokens=0, cache_read_tokens=0,
                                 cost_usd=seeded_cost)

        def strat(client, cfg, j, b, a, cycle="intraday", extra_context=""):
            return AgentResult(final_text="", drafts=[], iterations=1, stop_reason="end_turn")

        orch = Orchestrator(config, journal, StubBroker(acct(), market_open=True),
                            client=None, strategy_runner=strat,
                            risk_reviewer=lambda *a: None)
        return orch

    def test_over_cap_skips_intraday(self, tmp_path):
        orch = self._orch(tmp_path, cap=0.5, seeded_cost=1.0)
        report = orch.run_cycle("intraday")
        assert report.skipped == "cost cap reached"

    def test_under_cap_runs(self, tmp_path):
        orch = self._orch(tmp_path, cap=5.0, seeded_cost=1.0)
        report = orch.run_cycle("intraday")
        assert report.skipped is None


class TestPreflight:
    def test_missing_anthropic_is_no_go(self, tmp_path):
        from trading.preflight import run_preflight
        config = make_config()
        config.settings.paths.journal_db = str(tmp_path / "j.db")
        r = run_preflight(config, broker_factory=lambda c: StubBroker(make_account()))
        names = {c.name: c.ok for c in r.checks}
        assert names["broker"] is True
        assert names["market-data"] is True
        assert names["anthropic"] is False
        assert r.critical_ok is False

    def test_all_present_is_go(self, tmp_path):
        from trading.preflight import run_preflight
        config = make_config()
        config.settings.paths.journal_db = str(tmp_path / "j.db")
        config.secrets.anthropic_api_key = "x"
        r = run_preflight(config, broker_factory=lambda c: StubBroker(make_account()))
        assert r.critical_ok is True

    def test_no_broker_is_no_go(self, tmp_path):
        from trading.preflight import run_preflight
        config = make_config()
        config.settings.paths.journal_db = str(tmp_path / "j.db")
        config.secrets.anthropic_api_key = "x"
        r = run_preflight(config, broker_factory=lambda c: None)
        assert {c.name: c.ok for c in r.checks}["broker"] is False
        assert r.critical_ok is False


class TestNotifyNoOp:
    def test_fill_and_event_noop_without_webhook(self, tmp_path):
        from trading import notify
        from trading.broker.sync import SyncReport
        config = make_config()  # no webhook
        journal = Journal(tmp_path / "j.db")
        notify.notify_fill(config, journal, SyncReport(fills_recorded=1))
        notify.notify_event(config, journal, "test", "detail")  # no exception

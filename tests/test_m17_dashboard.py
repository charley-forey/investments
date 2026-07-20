"""M17: operations dashboard API + equity history."""

from fastapi.testclient import TestClient

from conftest import make_config

from stubs import StubBroker, make_account

from trading.broker.models import PositionView
from trading.data.journal import Journal


def client_and_journal(tmp_path, positions=None, mode="paper"):
    config = make_config(mode=mode)
    config.settings.paths.journal_db = str(tmp_path / "j.db")
    config.settings.paths.intel_db = str(tmp_path / "intel.db")
    journal = Journal(config.settings.paths.journal_db)
    account = make_account(positions=positions or [])
    from trading.web.app import create_app
    app = create_app(get_config_fn=lambda: config,
                     broker_factory=lambda c: StubBroker(account))
    return TestClient(app), journal, config


class TestEquityHistory:
    def test_record_and_read(self, tmp_path):
        journal = Journal(tmp_path / "j.db")
        journal.record_equity(equity=100000, cash=50000, buying_power=200000)
        journal.record_equity(equity=101000, cash=50000, buying_power=200000)
        hist = journal.equity_history()
        assert len(hist) == 2
        assert hist[-1]["equity"] == 101000  # chronological order


class TestReadEndpoints:
    def test_account(self, tmp_path):
        client, _, _ = client_and_journal(tmp_path)
        r = client.get("/api/account")
        assert r.status_code == 200
        assert r.json()["available"] is True
        assert r.json()["equity"] == 50000

    def test_account_records_equity(self, tmp_path):
        client, journal, _ = client_and_journal(tmp_path)
        client.get("/api/account")
        assert len(journal.equity_history()) >= 1  # throttled snapshot recorded

    def test_positions(self, tmp_path):
        pos = [PositionView(symbol="AAPL", qty=10, avg_entry_price=100.0,
                            market_value=1050.0, unrealized_pl=50.0)]
        client, _, _ = client_and_journal(tmp_path, positions=pos)
        r = client.get("/api/positions")
        assert r.json()["positions"][0]["symbol"] == "AAPL"

    def test_pnl(self, tmp_path):
        client, journal, _ = client_and_journal(
            tmp_path, positions=[PositionView(symbol="X", qty=1, avg_entry_price=1,
                                              market_value=1, unrealized_pl=25.0)])
        journal.record_score(strategy_tag="t", pnl_usd=100.0, term="short")
        r = client.get("/api/pnl").json()
        assert r["realized"] == 100.0
        assert r["unrealized"] == 25.0
        assert r["total"] == 125.0

    def test_trades(self, tmp_path):
        client, journal, _ = client_and_journal(tmp_path)
        oid = journal.record_order(proposal_id=None, mode="paper", symbol="AAPL",
                                   side="buy", qty=10, order_type="limit", limit_price=100.0)
        journal.record_fill(oid, qty=10, price=100.0)
        rows = client.get("/api/trades").json()
        assert rows[0]["symbol"] == "AAPL" and len(rows[0]["fills"]) == 1

    def test_opportunities(self, tmp_path):
        client, journal, _ = client_and_journal(tmp_path)
        pid = journal.record_proposal(agent="strategy", strategy_tag="t", symbol="NVDA",
                                      asset_class="stock", side="buy", qty=1,
                                      order_type="limit", limit_price=100.0, thesis="AI")
        journal.record_verdict(pid, source="risk_agent", verdict="veto", reason="too rich")
        journal.set_proposal_status(pid, "vetoed")
        rows = client.get("/api/opportunities").json()
        assert rows[0]["status"] == "vetoed"
        assert "too rich" in rows[0]["reason"]

    def test_equity_endpoint(self, tmp_path):
        client, journal, _ = client_and_journal(tmp_path)
        journal.record_equity(equity=100000)
        assert client.get("/api/equity").json()[0]["equity"] == 100000

    def test_performance_and_decisions(self, tmp_path):
        client, journal, _ = client_and_journal(tmp_path)
        assert client.get("/api/performance").status_code == 200
        assert client.get("/api/decisions").status_code == 200
        assert client.get("/api/watchlist").status_code == 200
        assert client.get("/api/sentiment").json() == []  # no intel db yet

    def test_index_served(self, tmp_path):
        client, _, _ = client_and_journal(tmp_path)
        r = client.get("/")
        assert r.status_code == 200
        assert "Agentic Trading" in r.text and "Opportunities" in r.text


class TestConfig:
    def test_view_and_validate(self, tmp_path):
        client, _, _ = client_and_journal(tmp_path)
        cfg = client.get("/api/config").json()
        assert "limits" in cfg and "settings" in cfg
        ok = client.post("/api/config/validate",
                         json={"section": "settings", "data": cfg["settings"]})
        assert ok.json()["ok"] is True
        bad = client.post("/api/config/validate", json={"section": "limits", "data": {}})
        assert bad.json()["ok"] is False


class TestActions:
    def test_reset_kill_switch(self, tmp_path):
        client, journal, _ = client_and_journal(tmp_path)
        journal.trip_kill_switch("test")
        assert journal.kill_switch_active()
        r = client.post("/api/actions/reset-kill-switch")
        assert r.json()["ok"] is True
        assert not journal.kill_switch_active()

    def test_approve_pending(self, tmp_path):
        client, journal, _ = client_and_journal(tmp_path)
        pid = journal.record_proposal(agent="strategy", strategy_tag="t", symbol="AAPL",
                                      asset_class="stock", side="buy", qty=5,
                                      order_type="limit", limit_price=100.0)
        journal.set_proposal_status(pid, "pending_approval")
        r = client.post(f"/api/actions/approve/{pid}")
        assert r.json()["ok"] is True and r.json()["status"] == "submitted"

    def test_run_cycle_returns_started(self, tmp_path):
        client, _, _ = client_and_journal(tmp_path)
        r = client.post("/api/actions/run-cycle", json={"cycle": "premarket"})
        assert r.json()["started"] == "premarket"

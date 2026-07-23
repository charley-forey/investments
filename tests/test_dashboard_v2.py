"""Dashboard v2: the endpoints the rebuilt console added, plus the static bundle.

Every one of these backs a specific panel, so a break here is a blank card in the
UI rather than a loud failure — hence the tests.
"""

import json

from fastapi.testclient import TestClient

from conftest import make_config
from stubs import StubBroker, make_account

from trading.data.journal import Journal
from trading.web.app import _yaml_help, create_app


def snap(journal, symbol, **kw):
    kw.setdefault("features", None)
    return journal.record_snapshot(cycle="intraday", symbol=symbol, bid=1, ask=2,
                                   last=1.5, spread_bps=5.0, sentiment=None,
                                   mention_count=None, **kw)


def prop(journal, symbol, **kw):
    return journal.record_proposal(agent="trader", strategy_tag="t", symbol=symbol,
                                   asset_class="equity", side="buy", qty=1,
                                   order_type="limit", limit_price=1.5, **kw)


def client_and_journal(tmp_path, bars=None):
    config = make_config()
    config.settings.paths.journal_db = str(tmp_path / "j.db")
    config.settings.paths.intel_db = str(tmp_path / "intel.db")
    config.settings.paths.bars_db = str(tmp_path / "bars.db")
    journal = Journal(config.settings.paths.journal_db)
    broker = StubBroker(make_account())
    if bars is not None:
        broker.get_bars = lambda symbol, days=30, timeframe="1Day": bars
    app = create_app(get_config_fn=lambda: config, broker_factory=lambda c: broker)
    return TestClient(app), journal, config


class TestObservabilityEndpoints:
    def test_usage_totals(self, tmp_path):
        client, journal, _ = client_and_journal(tmp_path)
        journal.record_usage(cycle="intraday", agent="a", model="m", input_tokens=10,
                             output_tokens=5, cache_read_tokens=0, cost_usd=0.25)
        r = client.get("/api/usage").json()
        assert r["total_usd"] == 0.25
        assert r["rows"][0]["cycle"] == "intraday"

    def test_heartbeats(self, tmp_path):
        client, journal, _ = client_and_journal(tmp_path)
        journal.heartbeat("cycle:intraday", status="ok", detail="fine")
        rows = client.get("/api/heartbeats").json()
        assert rows[0]["job"] == "cycle:intraday"

    def test_funnel_stages_are_monotonic_in_meaning(self, tmp_path):
        client, journal, _ = client_and_journal(tmp_path)
        snap(journal, "AAPL")
        pid = prop(journal, "AAPL")
        journal.record_verdict(proposal_id=pid, source="guardrail", verdict="veto",
                               rule="max_position", reason="too big")
        stages = {s["stage"]: s["count"] for s in client.get("/api/funnel").json()["stages"]}
        assert stages["Symbols examined"] == 1
        assert stages["Proposals raised"] == 1
        assert stages["Cleared guardrails"] == 0   # the veto is subtracted
        assert stages["Orders submitted"] == 0

    def test_verdicts_group_by_rule(self, tmp_path):
        client, journal, _ = client_and_journal(tmp_path)
        pid = prop(journal, "X")
        journal.record_verdict(proposal_id=pid, source="g", verdict="veto",
                               rule="pdt", reason="4th day trade")
        r = client.get("/api/verdicts").json()
        assert r["by_rule"][0]["rule"] == "pdt"
        assert r["recent"][0]["reason"] == "4th day trade"


class TestSymbolRollup:
    def test_gathers_every_source(self, tmp_path):
        client, journal, _ = client_and_journal(tmp_path)
        snap(journal, "NVDA", features={"momentum_20": 0.1})
        prop(journal, "NVDA", thesis="up")
        d = client.get("/api/symbol/nvda").json()
        assert d["symbol"] == "NVDA"
        assert d["snapshots"][0]["features"]["momentum_20"] == 0.1
        assert d["proposals"][0]["thesis"] == "up"
        assert d["news"] == []          # no intel db -> empty, not an error

    def test_unknown_symbol_is_empty_not_an_error(self, tmp_path):
        client, _, _ = client_and_journal(tmp_path)
        d = client.get("/api/symbol/ZZZZ").json()
        assert d["proposals"] == [] and d["orders"] == [] and d["lots"] == []


class TestBars:
    def test_falls_back_to_broker_and_caches(self, tmp_path):
        import pandas as pd
        df = pd.DataFrame({
            "symbol": ["AAPL"] * 6,
            "timestamp": pd.to_datetime([f"2026-07-{d:02d}" for d in range(1, 7)], utc=True),
            "open": [1, 2, 3, 4, 5, 6], "high": [2, 3, 4, 5, 6, 7],
            "low": [0.5, 1, 2, 3, 4, 5], "close": [1.5, 2.5, 3.5, 4.5, 5.5, 6.5],
            "volume": [10] * 6,
        }).set_index(["symbol", "timestamp"])
        client, _, config = client_and_journal(tmp_path, bars=df)
        r = client.get("/api/bars/AAPL").json()
        assert len(r["bars"]) == 6
        assert r["bars"][0]["date"] == "2026-07-01"
        # Second call is served from the cache the first one populated.
        from trading.data.bars import BarStore
        store = BarStore(config.settings.paths.bars_db)
        assert len(store.load_bars("AAPL")) == 6
        store.close()

    def test_broker_failure_yields_empty_not_500(self, tmp_path):
        client, _, _ = client_and_journal(tmp_path)
        r = client.get("/api/bars/AAPL")
        assert r.status_code == 200
        assert r.json()["bars"] == []


class TestConfigEditor:
    def test_schema_carries_bounds_and_help(self, tmp_path):
        client, _, _ = client_and_journal(tmp_path)
        r = client.get("/api/config/schema").json()
        pos = r["limits"]["schema"]["$defs"]["PositionLimits"]["properties"]
        assert pos["max_position_pct"]["maximum"] == 100      # drives the form's min/max
        assert isinstance(r["limits"]["help"], dict)

    def test_yaml_comments_become_help_text(self, tmp_path):
        p = tmp_path / "c.yaml"
        p.write_text("position:\n  max_position_pct: 10.0  # max % of equity per position\n"
                     "  plain: 1\n", encoding="utf-8")
        assert _yaml_help(p)["max_position_pct"] == "max % of equity per position"
        assert "plain" not in _yaml_help(p)

    def test_save_writes_a_backup_first(self, tmp_path, monkeypatch):
        client, _, config = client_and_journal(tmp_path)
        cfgdir = tmp_path / "config"
        cfgdir.mkdir()
        (cfgdir / "limits.yaml").write_text("mode: paper\n", encoding="utf-8")
        monkeypatch.setattr("trading.config.PROJECT_ROOT", tmp_path)
        data = client.get("/api/config").json()["limits"]
        r = client.post("/api/config/save", json={"section": "limits", "data": data}).json()
        assert r["ok"] and r["backup"]
        backups = list((tmp_path / "backups" / "config").glob("limits.*.yaml"))
        assert backups and backups[0].read_text(encoding="utf-8") == "mode: paper\n"

    def test_invalid_config_is_rejected_before_any_write(self, tmp_path):
        client, _, _ = client_and_journal(tmp_path)
        data = client.get("/api/config").json()["limits"]
        data["position"]["max_position_pct"] = 900      # schema says <= 100
        r = client.post("/api/config/save", json={"section": "limits", "data": data}).json()
        assert r["ok"] is False and r["errors"]


class TestStaticBundle:
    def test_the_console_and_its_assets_are_served(self, tmp_path):
        client, _, _ = client_and_journal(tmp_path)
        html = client.get("/").text
        assert "/static/js/app.js" in html and "/static/css/app.css" in html
        for path in ("/static/css/app.css", "/static/js/app.js", "/static/js/charts.js"):
            assert client.get(path).status_code == 200

    def test_every_view_endpoint_the_console_calls_exists(self, tmp_path):
        """The console fetches these by string; a rename would silently blank a panel."""
        client, _, _ = client_and_journal(tmp_path)
        for path in ("/api/metrics", "/api/account", "/api/pnl", "/api/equity",
                     "/api/intel", "/api/heartbeats", "/api/portfolio_risk", "/api/config",
                     "/api/config/schema", "/api/opportunities", "/api/usage",
                     "/api/positions", "/api/trades", "/api/signals", "/api/news",
                     "/api/sentiment", "/api/watchlist", "/api/funnel", "/api/verdicts",
                     "/api/cycle-log", "/api/decisions", "/api/performance", "/api/edge"):
            assert client.get(path).status_code == 200, path

    def test_console_js_references_only_real_endpoints(self, tmp_path):
        """Catch a typo'd /api/... path in the frontend without opening a browser."""
        import re
        from pathlib import Path
        client, _, _ = client_and_journal(tmp_path)
        js = Path(__file__).parent.parent / "src" / "trading" / "web" / "static" / "js" / "app.js"
        known = {r.path for r in client.app.routes if getattr(r, "path", "").startswith("/api")}
        for raw in set(re.findall(r"['\"`](/api/[^'\"`?$]*)", js.read_text(encoding="utf-8"))):
            path = raw.rstrip("/")
            # Templated calls ("/api/symbol/" + sym) match their parameterised route.
            assert any(path == k or k.startswith(path) for k in known), path

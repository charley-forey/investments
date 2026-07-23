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


class TestScaleAndAggregation:
    def test_trades_uses_two_queries_regardless_of_order_count(self, tmp_path):
        """The fills lookup was one query per order; assert it no longer scales
        with the number of orders."""
        client, journal, _ = client_and_journal(tmp_path)
        for i in range(25):
            oid = journal.record_order(proposal_id=None, broker_order_id=f"b{i}",
                                       mode="paper", symbol="AAPL", side="buy", qty=1,
                                       order_type="limit", limit_price=10.0)
            journal.record_fill(order_id=oid, qty=1, price=10.0)

        seen = []
        real = journal.conn.execute

        import trading.web.app as appmod
        orig = appmod.Journal

        class Counting(orig):
            def __init__(self, *a, **k):
                super().__init__(*a, **k)
                outer = self

                class Conn:
                    def __getattr__(self, n):
                        return getattr(outer._raw, n)

                    def execute(self, sql, *a):
                        seen.append(sql)
                        return outer._raw.execute(sql, *a)
                self._raw = self.conn
                self.conn = Conn()

        appmod.Journal = Counting
        try:
            rows = client.get("/api/trades").json()
        finally:
            appmod.Journal = orig
        assert len(rows) == 25
        assert all(len(r["fills"]) == 1 for r in rows)
        fill_queries = [s for s in seen if "FROM fills" in s]
        assert len(fill_queries) == 1, f"expected one batched fills query, got {len(fill_queries)}"
        assert real is not None

    def test_signals_latest_returns_one_row_per_symbol(self, tmp_path):
        client, journal, _ = client_and_journal(tmp_path)
        for _ in range(3):
            snap(journal, "AAPL")
        snap(journal, "NVDA")
        rows = client.get("/api/signals/latest").json()
        assert [r["symbol"] for r in rows] == ["AAPL", "NVDA"]

    def test_signals_grid_buckets_server_side(self, tmp_path):
        client, journal, _ = client_and_journal(tmp_path)
        snap(journal, "AAPL", features={"momentum_20": 0.2})
        snap(journal, "AAPL", features={"momentum_20": 0.4})
        g = client.get("/api/signals/grid?metric=momentum_20&days=7&points=4").json()
        assert len(g["cols"]) == 4
        assert g["rows"][0]["label"] == "AAPL"
        vals = [c["v"] for c in g["rows"][0]["cells"] if c["v"] is not None]
        assert vals and abs(vals[-1] - 0.3) < 1e-9      # both rows averaged in one bucket

    def test_signals_grid_rejects_an_unknown_metric(self, tmp_path):
        """The metric is interpolated into SQL, so it must be whitelisted."""
        client, _, _ = client_and_journal(tmp_path)
        r = client.get("/api/signals/grid?metric=1);DROP TABLE proposals;--")
        assert r.status_code == 400

    def test_hot_read_paths_are_indexed(self, tmp_path):
        _, journal, _ = client_and_journal(tmp_path)
        names = {r["name"] for r in journal.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'")}
        assert {"ix_fills_order", "ix_snapshot_symbol_id", "ix_equity_ts"} <= names


class TestOutcomes:
    def test_reports_why_nothing_is_graded_yet(self, tmp_path):
        """An empty grading table is usually 'not eligible yet', not a fault —
        the payload has to say which."""
        client, journal, _ = client_and_journal(tmp_path)
        pid = prop(journal, "AAPL")
        journal.set_proposal_status(pid, "vetoed")
        d = client.get("/api/outcomes").json()
        assert d["outcomes"] == []
        waiting = d["pipeline"]["waiting"]
        assert waiting[0]["id"] == pid
        assert waiting[0]["blocked_by"] == "not yet aged"
        assert waiting[0]["gradable_on"] > waiting[0]["ts"]

    def test_surfaces_graded_outcomes_and_veto_quality(self, tmp_path):
        client, journal, _ = client_and_journal(tmp_path)
        pid = prop(journal, "AAPL", confidence=0.9)
        journal.set_proposal_status(pid, "vetoed")
        journal.record_proposal_outcome(
            proposal_id=pid, horizon_days=5, entry_price=10.0, stop_price=9.0,
            target_price=12.0, max_favorable_usd=5.0, max_adverse_usd=-20.0,
            hypothetical_pnl=-15.0, stop_hit=True, target_hit=False,
            verdict_was_right=True, notes="stop would have hit")
        d = client.get("/api/outcomes").json()
        assert d["outcomes"][0]["symbol"] == "AAPL"
        assert d["outcomes"][0]["verdict_was_right"] == 1
        assert d["calibration"]["veto_hit_rate"] == 1.0
        assert d["pipeline"]["waiting"] == []

    def test_run_scoring_action_is_deterministic_and_safe(self, tmp_path):
        client, _, _ = client_and_journal(tmp_path)
        r = client.post("/api/actions/run-scoring").json()
        assert r["ok"] and r["scored"] == 0 and r["graded"] == 0


class TestAuth:
    def test_loopback_without_a_token_stays_open(self, tmp_path):
        client, _, _ = client_and_journal(tmp_path)
        assert client.get("/api/metrics").status_code == 200
        assert client.get("/api/auth").json()["required"] is False

    def test_a_configured_token_is_required(self, tmp_path):
        config = make_config()
        config.settings.paths.journal_db = str(tmp_path / "j.db")
        app = create_app(get_config_fn=lambda: config,
                         broker_factory=lambda c: None, token="s3cret")
        client = TestClient(app)
        assert client.get("/api/metrics").status_code == 401
        assert client.post("/api/actions/reset-kill-switch").status_code == 401
        ok = client.get("/api/metrics", headers={"X-Dashboard-Token": "s3cret"})
        assert ok.status_code == 200
        bearer = client.get("/api/metrics", headers={"Authorization": "Bearer s3cret"})
        assert bearer.status_code == 200
        assert client.get("/api/metrics", headers={"X-Dashboard-Token": "wrong"}).status_code == 401

    def test_the_shell_loads_unauthenticated_so_it_can_ask_for_the_token(self, tmp_path):
        config = make_config()
        config.settings.paths.journal_db = str(tmp_path / "j.db")
        client = TestClient(create_app(get_config_fn=lambda: config,
                                       broker_factory=lambda c: None, token="s3cret"))
        assert client.get("/").status_code == 200
        assert client.get("/static/js/app.js").status_code == 200
        assert client.get("/api/auth").json() == {"required": True, "authenticated": False,
                                                  "loopback": True}

    def test_non_loopback_without_a_token_is_refused(self, tmp_path):
        """Fail closed: these endpoints submit orders."""
        config = make_config()
        config.settings.paths.journal_db = str(tmp_path / "j.db")
        client = TestClient(create_app(get_config_fn=lambda: config,
                                       broker_factory=lambda c: None),
                            client=("10.0.0.7", 5000))
        r = client.get("/api/metrics")
        assert r.status_code == 403 and "token" in r.json()["detail"]

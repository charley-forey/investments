"""M16: reasoning capture, Decision Record, and the observability dashboard."""

from conftest import make_config, make_quote

from stubs import (
    ScriptedClient, StubBroker, _Response, make_account, text_block, tool_use_block,
)

from trading.agents.runner import run_agent
from trading.analytics.decision_record import build_record, list_records
from trading.data.journal import Journal
from trading.tools.registry import STRATEGY_TOOLS, ToolContext, ToolRegistry


class _Thinking:
    type = "thinking"
    def __init__(self, t): self.thinking = t


class TestReasoningCapture:
    def test_thinking_and_tools_captured(self, tmp_path):
        config = make_config()
        journal = Journal(tmp_path / "j.db")
        ctx = ToolContext(config=config, journal=journal, broker=StubBroker(make_account()),
                          account_state=make_account(), agent_name="strategy")
        reg = ToolRegistry(ctx, STRATEGY_TOOLS)
        responses = [
            _Response([_Thinking("AAPL looks strong on the breakout"),
                       tool_use_block("t1", "get_account_state", {})], "tool_use"),
            _Response([_Thinking("edge clears costs; proposing"),
                       text_block("done")], "end_turn"),
        ]
        result = run_agent(ScriptedClient(responses), model="m", max_tokens=100,
                           system_prompt="s", registry=reg, user_message="scan",
                           max_iterations=5, journal=journal)
        assert "breakout" in result.reasoning
        assert "clears costs" in result.reasoning
        assert result.tool_calls[0]["name"] == "get_account_state"

    def test_reasoning_persisted_and_retrievable(self, tmp_path):
        journal = Journal(tmp_path / "j.db")
        pid = journal.record_proposal(agent="strategy", strategy_tag="t", symbol="AAPL",
                                      asset_class="stock", side="buy", qty=10,
                                      order_type="limit", limit_price=100.0)
        journal.record_reasoning(proposal_id=pid, agent="strategy",
                                 reasoning="thesis: breakout on volume",
                                 tool_calls=[{"name": "get_bars", "input": {}}])
        rows = journal.reasoning_for(pid)
        assert rows[0]["reasoning"] == "thesis: breakout on volume"


class TestDecisionRecord:
    def _seed(self, tmp_path):
        journal = Journal(tmp_path / "j.db")
        pid = journal.record_proposal(agent="strategy", strategy_tag="news-breakout",
                                      symbol="AAPL", asset_class="stock", side="buy",
                                      qty=10, order_type="limit", limit_price=100.0,
                                      thesis="catalyst breakout", expected_edge_usd=80.0,
                                      confidence=0.7)
        journal.record_reasoning(proposal_id=pid, agent="strategy",
                                 reasoning="strong volume, clean catalyst",
                                 tool_calls=[{"name": "get_news", "input": {}}])
        journal.record_verdict(pid, source="risk_agent", verdict="approve", reason="ok")
        journal.set_proposal_status(pid, "submitted")
        journal.record_order(proposal_id=pid, mode="paper", symbol="AAPL", side="buy",
                             qty=10, order_type="limit", limit_price=100.0)
        journal.record_score(proposal_id=pid, strategy_tag="news-breakout", pnl_usd=50.0,
                             term="short", grade="win")
        return journal, pid

    def test_full_record_chain(self, tmp_path):
        journal, pid = self._seed(tmp_path)
        rec = build_record(journal, pid)
        assert rec.symbol == "AAPL"
        assert "catalyst breakout" in rec.thesis
        assert "strong volume" in rec.reasoning
        assert rec.tool_calls[0]["name"] == "get_news"
        assert any(v["source"] == "risk_agent" for v in rec.verdicts)
        assert rec.orders and rec.scores
        text = rec.full_text()
        assert "Agent reasoning" in text and "Outcome" in text

    def test_list_and_missing(self, tmp_path):
        journal, pid = self._seed(tmp_path)
        assert list_records(journal)[0].proposal_id == pid
        assert build_record(journal, 9999) is None


class TestDashboard:
    def _client(self, tmp_path):
        from fastapi.testclient import TestClient
        from trading.web.app import create_app

        config = make_config()
        config.settings.paths.journal_db = str(tmp_path / "j.db")
        journal = Journal(config.settings.paths.journal_db)
        journal.heartbeat("cycle:intraday", status="end", detail="ok")
        return TestClient(create_app(get_config_fn=lambda: config)), journal, config

    def test_metrics_endpoint(self, tmp_path):
        client, _, _ = self._client(tmp_path)
        r = client.get("/api/metrics")
        assert r.status_code == 200
        assert r.json()["mode"] == "paper"

    def test_decisions_endpoint(self, tmp_path):
        client, journal, _ = self._client(tmp_path)
        journal.record_proposal(agent="strategy", strategy_tag="t", symbol="AAPL",
                                asset_class="stock", side="buy", qty=1, order_type="limit",
                                limit_price=100.0)
        r = client.get("/api/decisions")
        assert r.status_code == 200 and len(r.json()) == 1

    def test_index_html(self, tmp_path):
        client, _, _ = self._client(tmp_path)
        r = client.get("/")
        assert r.status_code == 200 and "Observability" in r.text

    def test_config_view_and_validate(self, tmp_path):
        client, _, _ = self._client(tmp_path)
        assert client.get("/api/config").status_code == 200
        # valid settings section
        good = client.post("/api/config/validate",
                           json={"section": "settings", "data": client.get("/api/config").json()["settings"]})
        assert good.json()["ok"] is True
        # invalid: bad tax rate type
        bad = client.post("/api/config/validate", json={"section": "limits", "data": {}})
        assert bad.json()["ok"] is False

    def test_query_endpoint(self, tmp_path):
        client, journal, _ = self._client(tmp_path)
        journal.record_proposal(agent="strategy", strategy_tag="t", symbol="NVDA",
                                asset_class="stock", side="buy", qty=1, order_type="limit",
                                limit_price=100.0, thesis="AI demand")
        r = client.get("/api/query", params={"q": "NVDA"})
        assert r.status_code == 200 and r.json()[0]["symbol"] == "NVDA"

"""Phase A paper-proof: failure context, playbook auto-apply, calendar, signal research."""

from __future__ import annotations

from conftest import make_config

from trading.agents.strategy import _recent_failures_context
from trading.analytics.playbooks import (
    apply_playbook_edits, parse_playbook_edits,
)
from trading.analytics.signal_research_runner import run_signal_research
from trading.data.calendar_feed import refresh_calendar
from trading.data.journal import Journal
from trading.paper_proof import run_paper_proof


class TestRecentFailures:
    def test_surfaces_veto_and_reject(self, tmp_path):
        j = Journal(tmp_path / "j.db")
        pid = j.record_proposal(
            agent="strategy", symbol="META", asset_class="stock", side="buy",
            qty=40, order_type="limit", strategy_tag="t", limit_price=650.0,
            stop_price=630.0, thesis="big", expected_edge_usd=200.0,
        )
        j.set_proposal_status(pid, "rejected")
        j.record_verdict(pid, source="guardrail", verdict="reject",
                         rule="max_order_notional", reason="notional $26100 > cap $5000")
        text = _recent_failures_context(j)
        assert "META" in text
        assert "max_order_notional" in text
        assert "26100" in text or "26,100" in text


class TestPlaybookAutoApply:
    def test_parse_fence(self):
        md = """
## Notes
Tighten filters.

```playbook-edits
{"edits": [{"tag": "news-breakout", "action": "append_bullets",
 "section": "Entry filters", "bullets": ["Require spread < 0.4%"]}]}
```
"""
        edits = parse_playbook_edits(md)
        assert len(edits) == 1
        assert edits[0].tag == "news-breakout"
        assert edits[0].action == "append_bullets"

    def test_paper_applies_append(self, tmp_path):
        pb = tmp_path / "playbooks"
        pb.mkdir()
        (pb / "news-breakout.md").write_text(
            "# Playbook: news-breakout\n\n## Entry filters\n- Old rule\n",
            encoding="utf-8",
        )
        md = """```playbook-edits
{"edits": [{"tag": "news-breakout", "action": "append_bullets",
 "section": "Entry filters", "bullets": ["- Require spread < 0.4% of mid"]}]}
```"""
        report = apply_playbook_edits(
            md, playbooks_dir=pb, memory_dir=tmp_path / "memory", paper_mode=True,
        )
        assert report.applied
        body = (pb / "news-breakout.md").read_text(encoding="utf-8")
        assert "spread < 0.4%" in body
        assert "Old rule" in body

    def test_live_queues_instead_of_applying(self, tmp_path):
        pb = tmp_path / "playbooks"
        pb.mkdir()
        (pb / "news-breakout.md").write_text("# Playbook: news-breakout\n", encoding="utf-8")
        md = """```playbook-edits
{"edits": [{"tag": "news-breakout", "action": "append_bullets",
 "section": "Entry filters", "bullets": ["- new"]}]}
```"""
        report = apply_playbook_edits(
            md, playbooks_dir=pb, memory_dir=tmp_path / "memory", paper_mode=False,
        )
        assert report.queued
        assert not report.applied
        pending = (tmp_path / "memory" / "pending_playbook_edits.md").read_text(encoding="utf-8")
        assert "playbook-edits" in pending
        assert "- new" not in (pb / "news-breakout.md").read_text(encoding="utf-8")

    def test_create_action(self, tmp_path):
        pb = tmp_path / "playbooks"
        md = """```playbook-edits
{"edits": [{"tag": "rel-strength-long", "action": "create",
 "bullets": ["- RS vs QQQ over 20d"]}]}
```"""
        report = apply_playbook_edits(
            md, playbooks_dir=pb, memory_dir=tmp_path / "memory", paper_mode=True,
        )
        assert any("created" in a for a in report.applied)
        assert (pb / "rel-strength-long.md").exists()


class TestCalendarRefresh:
    def test_merge_and_write(self, tmp_path):
        config = make_config()
        cal = tmp_path / "calendar.json"
        config.settings.paths.calendar_file = str(cal)
        config.settings.universe.core = ["AAPL", "META"]

        def fake_fetch(sym):
            return [{"date": "2099-01-15", "symbol": sym, "event": "earnings"}]

        report = refresh_calendar(config, fetch_fn=fake_fetch)
        assert report.events_written >= 2
        assert report.symbols_ok == 2
        data = __import__("json").loads(cal.read_text(encoding="utf-8"))
        syms = {e["symbol"] for e in data if e.get("symbol")}
        assert {"AAPL", "META"} <= syms
        assert any(e.get("event_type") == "macro" for e in data)


class TestSignalResearchRunner:
    def test_empty_intel_is_safe(self, tmp_path):
        config = make_config()
        config.settings.paths.intel_db = str(tmp_path / "missing.db")
        config.settings.paths.memory_dir = str(tmp_path / "memory")

        class B:
            def get_bars(self, symbol, days=30):
                return None

        report = run_signal_research(config, B())
        assert "No intel" in report.text or "No studies" in report.text


class TestPaperProof:
    def test_sizing_and_schema_pass(self, tmp_path, monkeypatch):
        config = make_config()
        config.settings.paths.journal_db = str(tmp_path / "j.db")
        config.settings.paths.calendar_file = str(tmp_path / "cal.json")
        config.settings.paths.playbooks_dir = str(tmp_path / "playbooks")
        (tmp_path / "playbooks").mkdir()
        (tmp_path / "playbooks" / "news-breakout.md").write_text("# x\n", encoding="utf-8")
        (tmp_path / "cal.json").write_text(
            '[{"date": "2099-01-01", "symbol": "AAPL", "event": "earnings"}]',
            encoding="utf-8",
        )
        # Force journal schema via open
        Journal(config.settings.paths.journal_db).close()

        def fake_preflight(cfg, broker_factory=None):
            from trading.preflight import PreflightResult
            r = PreflightResult()
            r.add("broker", True, "ok")
            r.add("anthropic", True, "ok")
            r.add("journal-db", True, "ok")
            return r

        monkeypatch.setattr("trading.paper_proof.run_preflight", fake_preflight)
        result = run_paper_proof(config, broker_factory=lambda c: None)
        assert any(c.name == "sizing-precheck" and c.ok for c in result.checks)
        assert any(c.name == "journal-schema" and c.ok for c in result.checks)
        assert any(c.name == "calendar" and c.ok for c in result.checks)
        assert result.ok

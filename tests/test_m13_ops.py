"""M13: JSON logging, metrics snapshot, inbound approval-command handling."""

import json
import logging

from conftest import make_config, make_quote

from stubs import StubBroker, make_account

from trading.approvals import handle_approval_command, parse_approval_command
from trading.data.journal import Journal
from trading.guardrails.engine import OrderPipeline
from trading.guardrails.models import OrderProposal
from trading.logging_setup import JsonFormatter, configure_logging
from trading.monitoring import metrics_snapshot


class TestJsonLogging:
    def test_json_formatter_emits_object(self):
        rec = logging.LogRecord("t", logging.INFO, __file__, 1, "hello", None, None)
        out = json.loads(JsonFormatter().format(rec))
        assert out["level"] == "INFO"
        assert out["msg"] == "hello"

    def test_configure_json(self):
        configure_logging(json_output=True)
        handler = logging.getLogger().handlers[0]
        assert isinstance(handler.formatter, JsonFormatter)
        configure_logging(json_output=False)  # restore


class TestMetricsSnapshot:
    def test_snapshot_shape(self, tmp_path):
        config = make_config()
        journal = Journal(tmp_path / "j.db")
        journal.heartbeat("cycle:intraday", status="end", detail="ok")
        snap = metrics_snapshot(config, journal)
        for key in ("mode", "healthy", "kill_switch", "cycles_24h", "cost_24h_usd",
                    "pending_approvals", "live_scale_level"):
            assert key in snap
        assert snap["mode"] == "paper"
        assert snap["cycles_24h"] == 1
        assert json.dumps(snap)  # serializable


class TestApprovalCommand:
    def test_parse_variants(self):
        assert parse_approval_command("approve 42").proposal_id == 42
        assert parse_approval_command("Approve #7").action == "approve"
        assert parse_approval_command("deny 3").action == "deny"
        assert parse_approval_command("hello") is None
        assert parse_approval_command(None) is None

    def _live_pending(self, tmp_path):
        from trading.analytics import lifecycle

        config = make_config(mode="live")
        journal = Journal(tmp_path / "j.db")
        lifecycle.set_stage(journal, "t", "small-live")
        pipeline = OrderPipeline(config, journal, StubBroker(make_account()))
        prop = OrderProposal(symbol="AAPL", side="buy", qty=5, order_type="limit",
                             limit_price=100.0, stop_price=95.0, expected_edge_usd=100.0,
                             strategy_tag="t")
        res = pipeline.process(prop, make_account(), make_quote(), market_is_open=True)
        assert res.status == "pending_approval"
        return pipeline, journal, res.proposal_id

    def test_approve_via_command(self, tmp_path):
        pipeline, journal, pid = self._live_pending(tmp_path)
        reply = handle_approval_command(pipeline, journal, f"approve {pid}")
        assert "approved" in reply
        assert journal.get_proposal(pid)["status"] == "submitted"

    def test_deny_via_command(self, tmp_path):
        pipeline, journal, pid = self._live_pending(tmp_path)
        reply = handle_approval_command(pipeline, journal, f"deny {pid}")
        assert "denied" in reply
        assert journal.get_proposal(pid)["status"] == "rejected"

    def test_non_command_ignored(self, tmp_path):
        pipeline, journal, pid = self._live_pending(tmp_path)
        assert handle_approval_command(pipeline, journal, "how's it going?") == ""

"""M11: cost-aware model routing + adversarial red-team pass."""

from conftest import make_config

from stubs import StubBroker, make_account

from trading.agents.redteam import should_red_team
from trading.agents.risk import RiskVerdict
from trading.agents.runner import AgentResult
from trading.data.journal import Journal
from trading.guardrails.models import OrderProposal
from trading.orchestrator import Orchestrator


def draft(confidence=None, **kw):
    d = dict(agent="strategy", strategy_tag="t", symbol="AAPL", asset_class="stock",
             side="buy", qty=10, order_type="limit", limit_price=100.0, stop_price=95.0,
             thesis="t", expected_edge_usd=100.0, confidence=confidence)
    d.update(kw)
    return OrderProposal(**d)


class TestModelRouting:
    def test_default_falls_back_to_model(self):
        s = make_config().settings.agents
        assert s.model_for("strategy") == s.model
        assert s.model_for("risk") == s.model

    def test_per_role_override(self):
        config = make_config()
        config.settings.agents.strategy_model = "claude-haiku-4-5"
        config.settings.agents.risk_model = "claude-opus-4-8"
        assert config.settings.agents.model_for("strategy") == "claude-haiku-4-5"
        assert config.settings.agents.model_for("risk") == "claude-opus-4-8"
        assert config.settings.agents.model_for("scoring") == config.settings.agents.model


class TestRedTeamTrigger:
    def test_disabled_by_default(self):
        config = make_config()  # threshold 1.0
        assert not should_red_team(config, draft(confidence=0.99))

    def test_triggers_above_threshold(self):
        config = make_config()
        config.settings.agents.redteam_confidence_threshold = 0.8
        assert should_red_team(config, draft(confidence=0.9))
        assert not should_red_team(config, draft(confidence=0.5))
        assert not should_red_team(config, draft(confidence=None))


class TestRedTeamInOrchestrator:
    def _orch(self, tmp_path, drafts, risk_verdict, redteam_verdict, threshold=0.8):
        config = make_config()
        config.settings.agents.redteam_confidence_threshold = threshold
        journal = Journal(tmp_path / "j.db")
        broker = StubBroker(make_account())

        def strategy_runner(client, cfg, jnl, brk, acct, cycle="intraday", extra_context=""):
            return AgentResult(final_text="", drafts=drafts, iterations=1, stop_reason="end_turn")

        def risk_reviewer(client, cfg, jnl, brk, acct, proposal):
            return risk_verdict

        def red_team_reviewer(client, cfg, jnl, brk, acct, proposal):
            return redteam_verdict

        return Orchestrator(config, journal, broker, client=None,
                            strategy_runner=strategy_runner, risk_reviewer=risk_reviewer,
                            red_team_reviewer=red_team_reviewer), journal

    def test_redteam_veto_blocks_risk_approved_trade(self, tmp_path):
        orch, journal = self._orch(
            tmp_path, [draft(confidence=0.95)],
            RiskVerdict("approve", "ok", []),
            RiskVerdict("veto", "crowded trade into earnings", ["gap risk"]),
        )
        report = orch.run_cycle("intraday")
        assert report.vetoed == 1
        assert report.submitted == 0
        veto = journal.conn.execute(
            "SELECT * FROM verdicts WHERE source='redteam' AND verdict='veto'").fetchone()
        assert "crowded" in veto["reason"]

    def test_redteam_approve_lets_trade_through(self, tmp_path):
        orch, journal = self._orch(
            tmp_path, [draft(confidence=0.95)],
            RiskVerdict("approve", "ok", []),
            RiskVerdict("approve", "no fatal flaw found", []),
        )
        report = orch.run_cycle("intraday")
        assert report.submitted == 1

    def test_low_confidence_skips_redteam(self, tmp_path):
        # confidence below threshold -> red-team not consulted; a veto verdict here
        # would be ignored because it's never called.
        orch, journal = self._orch(
            tmp_path, [draft(confidence=0.5)],
            RiskVerdict("approve", "ok", []),
            RiskVerdict("veto", "should not be consulted", []),
        )
        report = orch.run_cycle("intraday")
        assert report.submitted == 1  # traded despite the (unused) red-team veto

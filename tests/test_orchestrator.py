"""Orchestrator wiring with scripted strategy/risk agents (no real LLM)."""

from conftest import make_config

from stubs import StubBroker, make_account

from trading.agents.risk import RiskVerdict
from trading.agents.runner import AgentResult
from trading.data.journal import Journal
from trading.guardrails.models import OrderProposal
from trading.orchestrator import Orchestrator


def stock_draft(**kw) -> OrderProposal:
    defaults = dict(
        agent="strategy", strategy_tag="test", symbol="AAPL", asset_class="stock",
        side="buy", qty=10, order_type="limit", limit_price=100.0, stop_price=95.0,
        thesis="t", expected_edge_usd=100.0,
    )
    defaults.update(kw)
    return OrderProposal(**defaults)


def make_orch(tmp_path, drafts, verdict, market_open=True, kill=False):
    config = make_config()
    journal = Journal(tmp_path / "j.db")
    if kill:
        journal.trip_kill_switch("test")
    broker = StubBroker(make_account(), market_open=market_open)

    def strategy_runner(client, cfg, jnl, brk, acct, cycle="intraday", extra_context=""):
        return AgentResult(final_text="scan done", drafts=drafts, iterations=1,
                           stop_reason="end_turn")

    def risk_reviewer(client, cfg, jnl, brk, acct, proposal):
        return verdict

    orch = Orchestrator(config, journal, broker, client=None,
                        strategy_runner=strategy_runner, risk_reviewer=risk_reviewer)
    return orch, journal


def test_approved_draft_submits(tmp_path):
    orch, journal = make_orch(
        tmp_path, [stock_draft()],
        RiskVerdict(verdict="approve", reason="looks good", concerns=[]),
    )
    report = orch.run_cycle("intraday")
    assert report.proposals == 1
    assert report.submitted == 1
    assert report.vetoed == 0
    # A submitted order exists and carries a risk_agent approval verdict.
    order = journal.conn.execute("SELECT COUNT(*) AS n FROM orders").fetchone()["n"]
    assert order == 1
    verdicts = journal.conn.execute(
        "SELECT * FROM verdicts WHERE source='risk_agent' AND verdict='approve'"
    ).fetchall()
    assert len(verdicts) == 1


def test_vetoed_draft_never_reaches_pipeline(tmp_path):
    orch, journal = make_orch(
        tmp_path, [stock_draft()],
        RiskVerdict(verdict="veto", reason="thesis too weak", concerns=["no catalyst"]),
    )
    report = orch.run_cycle("intraday")
    assert report.vetoed == 1
    assert report.submitted == 0
    # No order; a veto verdict is journaled.
    assert journal.conn.execute("SELECT COUNT(*) AS n FROM orders").fetchone()["n"] == 0
    veto = journal.conn.execute(
        "SELECT * FROM verdicts WHERE source='risk_agent' AND verdict='veto'"
    ).fetchone()
    assert "thesis too weak" in veto["reason"]


def test_kill_switch_skips_trading(tmp_path):
    orch, journal = make_orch(
        tmp_path, [stock_draft()],
        RiskVerdict(verdict="approve", reason="x", concerns=[]), kill=True,
    )
    report = orch.run_cycle("intraday")
    assert report.skipped == "kill switch active"
    assert report.submitted == 0


def test_market_closed_skips_trading(tmp_path):
    orch, journal = make_orch(
        tmp_path, [stock_draft()],
        RiskVerdict(verdict="approve", reason="x", concerns=[]), market_open=False,
    )
    report = orch.run_cycle("intraday")
    assert report.skipped == "market closed"


def test_guardrail_still_rejects_after_risk_approve(tmp_path):
    # Risk approves, but the order is oversized -> guardrails reject it.
    orch, journal = make_orch(
        tmp_path, [stock_draft(qty=1000, limit_price=100.0)],  # $100k notional
        RiskVerdict(verdict="approve", reason="x", concerns=[]),
    )
    report = orch.run_cycle("intraday")
    assert report.submitted == 0
    assert report.rejected == 1

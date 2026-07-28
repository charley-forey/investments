"""M20: per-interval visibility — cycle narrative persisted every intraday cycle,
signal snapshot dataset, and the endpoints that surface them."""

from fastapi.testclient import TestClient

from conftest import make_config
from stubs import StubBroker, make_account

from trading.agents.risk import RiskVerdict
from trading.agents.runner import AgentResult
from trading.analytics.snapshot import snapshot_universe
from trading.data.journal import Journal
from trading.orchestrator import Orchestrator


def _orch_zero_proposals(tmp_path):
    """Intraday cycle whose strategy agent examines symbols but proposes nothing."""
    config = make_config()
    config.settings.paths.journal_db = str(tmp_path / "j.db")
    config.settings.paths.intel_db = str(tmp_path / "intel.db")
    journal = Journal(config.settings.paths.journal_db)
    broker = StubBroker(make_account())

    def strategy_runner(client, cfg, jnl, brk, acct, cycle="intraday", extra_context=""):
        return AgentResult(
            final_text="Examined the universe; spreads too wide, proposing nothing.",
            drafts=[], iterations=1, stop_reason="end_turn",
            tool_calls=[{"name": "get_quote", "input": {"symbol": "NVDA"}},
                        {"name": "get_bars", "input": {"symbol": "AAPL"}},
                        {"name": "get_quote", "input": {"symbol": "NVDA"}}],  # dup
        )

    orch = Orchestrator(config, journal, broker, client=None,
                        strategy_runner=strategy_runner,
                        risk_reviewer=lambda *a: RiskVerdict("approve", "x", []))
    return orch, journal, config


def test_zero_proposal_cycle_records_narrative(tmp_path):
    orch, journal, _ = _orch_zero_proposals(tmp_path)
    report = orch.run_cycle("intraday")
    assert report.proposals == 0
    log = journal.cycle_log()
    assert len(log) == 1
    assert log[0]["proposal_id"] is None
    assert "proposing nothing" in log[0]["reasoning"]


def test_cycle_does_not_snapshot_so_a_drain_tick_stays_cheap(tmp_path):
    """The intraday cycle runs every minute to drain the tick stream's wake queue.
    Snapshotting the universe from inside it would mean 88 quotes and 88 rows a
    minute — it is a separate scheduled job (run_snapshot_safe) for exactly that
    reason. This guards the cadence: putting it back makes the daemon a firehose."""
    orch, journal, config = _orch_zero_proposals(tmp_path)
    orch.run_cycle("intraday")
    assert journal.recent_snapshots() == []


def test_snapshot_universe_standalone(tmp_path):
    config = make_config()
    journal = Journal(tmp_path / "j.db")
    n = snapshot_universe(config, journal, StubBroker(make_account()))
    assert n == len(config.settings.universe.core)


def _client(tmp_path):
    config = make_config()
    config.settings.paths.journal_db = str(tmp_path / "j.db")
    config.settings.paths.intel_db = str(tmp_path / "intel.db")
    journal = Journal(config.settings.paths.journal_db)
    from trading.web.app import create_app
    app = create_app(get_config_fn=lambda: config,
                     broker_factory=lambda c: StubBroker(make_account()))
    return TestClient(app), journal


def test_cycle_log_endpoint_parses_symbols(tmp_path):
    client, journal = _client(tmp_path)
    journal.record_reasoning(
        proposal_id=None, agent="cycle:intraday", reasoning="looked, passed",
        tool_calls=[{"name": "get_quote", "input": {"symbol": "nvda"}},
                    {"name": "get_features", "input": {"symbol": "AAPL"}}])
    r = client.get("/api/cycle-log")
    assert r.status_code == 200
    row = r.json()[0]
    assert row["summary"] == "looked, passed"
    assert row["symbols_examined"] == ["NVDA", "AAPL"]  # de-duped, uppercased
    assert row["tool_count"] == 2


def test_signals_endpoint(tmp_path):
    client, journal = _client(tmp_path)
    journal.record_snapshot(cycle="intraday", symbol="NVDA", bid=100, ask=100.2,
                            last=100.1, spread_bps=20.0,
                            features={"momentum_20": 0.05, "realized_vol": 0.3},
                            sentiment=0.4, mention_count=12)
    r = client.get("/api/signals")
    assert r.status_code == 200
    row = r.json()[0]
    assert row["symbol"] == "NVDA"
    assert row["features"]["momentum_20"] == 0.05
    assert "features_json" not in row

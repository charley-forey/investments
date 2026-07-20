"""Track B integration: the orchestrator deterministically exits a stopped-out stock
position (no LLM) before the strategy agent runs."""

from conftest import make_config
from stubs import StubBroker, make_account

from trading.agents.risk import RiskVerdict
from trading.agents.runner import AgentResult
from trading.broker.models import PositionView
from trading.data.journal import Journal
from trading.orchestrator import Orchestrator


def _no_op_strategy(*a, **k):
    return AgentResult(final_text="nothing", drafts=[], iterations=1, stop_reason="end_turn")


def test_stopped_out_stock_is_closed(tmp_path):
    config = make_config()
    journal = Journal(tmp_path / "j.db")
    # Entry 120, current mark ~100 (StubBroker mid) => -16.7% <= -8% stop.
    pos = PositionView(symbol="AAPL", qty=10, avg_entry_price=120.0,
                       market_value=1000.0, unrealized_pl=-200.0, asset_class="stock")
    account = make_account(positions=[pos])
    broker = StubBroker(account)
    orch = Orchestrator(config, journal, broker, client=None,
                        strategy_runner=_no_op_strategy,
                        risk_reviewer=lambda *a: RiskVerdict("approve", "x", []))

    report = orch.run_cycle("intraday")

    assert any("exit AAPL" in n for n in report.notes), report.notes
    assert len(broker.submitted) == 1
    assert broker.submitted[0]["side"] == "sell"  # closing a long


def test_winning_position_within_bands_is_held(tmp_path):
    config = make_config()
    journal = Journal(tmp_path / "j.db")
    # Entry 99, mark ~100 => +1%: no stop, no target -> no exit.
    pos = PositionView(symbol="AAPL", qty=10, avg_entry_price=99.0,
                       market_value=1000.0, unrealized_pl=10.0, asset_class="stock")
    account = make_account(positions=[pos])
    broker = StubBroker(account)
    orch = Orchestrator(config, journal, broker, client=None,
                        strategy_runner=_no_op_strategy,
                        risk_reviewer=lambda *a: RiskVerdict("approve", "x", []))

    orch.run_cycle("intraday")
    assert len(broker.submitted) == 0  # held

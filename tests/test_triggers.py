"""Trigger gate + watchlist parsing."""

from datetime import datetime
from zoneinfo import ZoneInfo

from conftest import make_config
from stubs import StubBroker, make_account

from trading.broker.models import PositionView
from trading.triggers import (
    GateDecision, extract_and_save_from_watchlist, parse_triggers,
    should_run_intraday_llm,
)


SAMPLE = """
### 1. META — leader
Trigger: META above 656
### 2. NVDA
Trigger: NVDA near 203
### 3. AAPL
Trigger I want: hold the SMA — ignore this freeform line
Trigger: AAPL below 320
"""


def test_parse_triggers():
    triggers = parse_triggers(SAMPLE)
    assert {(t.symbol, t.direction, t.level) for t in triggers} == {
        ("META", "above", 656.0),
        ("NVDA", "near", 203.0),
        ("AAPL", "below", 320.0),
    }


def test_extract_and_save(tmp_path):
    config = make_config()
    config.settings.paths.journal_db = str(tmp_path / "j.db")
    triggers = extract_and_save_from_watchlist(config, SAMPLE)
    assert len(triggers) == 3
    path = tmp_path / "triggers.json"
    assert path.exists()


def test_gate_skips_when_quiet(tmp_path):
    config = make_config()
    config.settings.agents.trigger_gate_enabled = True
    config.settings.paths.journal_db = str(tmp_path / "j.db")
    from trading.data.journal import Journal
    journal = Journal(tmp_path / "j.db")
    broker = StubBroker(make_account())
    # Force a quiet midday-adjacent? Use a known non-forced hour: 11:00 ET.
    now = datetime(2026, 7, 21, 11, 0, tzinfo=ZoneInfo("America/New_York"))
    decision = should_run_intraday_llm(
        config, journal, broker, make_account(), now_et=now,
    )
    assert isinstance(decision, GateDecision)
    assert not decision.run_llm


def test_gate_forced_open_slot(tmp_path):
    config = make_config()
    config.settings.agents.trigger_gate_enabled = True
    config.settings.paths.journal_db = str(tmp_path / "j.db")
    from trading.data.journal import Journal
    journal = Journal(tmp_path / "j.db")
    broker = StubBroker(make_account())
    now = datetime(2026, 7, 21, 9, 45, tzinfo=ZoneInfo("America/New_York"))
    decision = should_run_intraday_llm(
        config, journal, broker, make_account(), now_et=now,
    )
    assert decision.run_llm
    assert "forced" in decision.reason


def test_gate_trigger_hit(tmp_path):
    config = make_config()
    config.settings.agents.trigger_gate_enabled = True
    config.settings.paths.journal_db = str(tmp_path / "j.db")
    extract_and_save_from_watchlist(config, "Trigger: META above 650\n")
    from trading.data.journal import Journal
    from trading.broker.models import Quote
    journal = Journal(tmp_path / "j.db")
    broker = StubBroker(make_account(), quotes={
        "META": Quote(symbol="META", bid=659.0, ask=661.0),
        "SPY": Quote(symbol="SPY", bid=500.0, ask=500.2),
    })
    now = datetime(2026, 7, 21, 11, 0, tzinfo=ZoneInfo("America/New_York"))
    decision = should_run_intraday_llm(
        config, journal, broker, make_account(), now_et=now,
    )
    assert decision.run_llm
    assert "META" in decision.reason


def test_model_by_cycle():
    config = make_config()
    config.settings.agents.model = "claude-opus-4-8"
    config.settings.agents.model_by_cycle = {
        "intraday": "claude-sonnet-4-6",
        "premarket": "claude-opus-4-8",
    }
    assert config.settings.agents.model_for("strategy", cycle="intraday") == "claude-sonnet-4-6"
    assert config.settings.agents.model_for("strategy", cycle="premarket") == "claude-opus-4-8"
    # Role override still wins when no cycle match? Plan: cycle checked first.
    config.settings.agents.strategy_model = "claude-haiku-4-5"
    assert config.settings.agents.model_for("strategy", cycle="intraday") == "claude-sonnet-4-6"
    assert config.settings.agents.model_for("strategy") == "claude-haiku-4-5"

"""Per-role tool assignment resolver."""

from conftest import make_config

from trading.config import AgentSettings
from trading.tools.assignment import (
    WEB_SEARCH, resolve_tools_for, web_search_tool_schema,
)
from trading.tools.registry import READ_ONLY_TOOLS, STRATEGY_TOOLS, TOOL_SCHEMAS


def test_default_strategy_gets_all_registry_tools():
    agents = AgentSettings()
    resolved = resolve_tools_for(agents, "strategy")
    assert set(resolved.registry) == set(STRATEGY_TOOLS)
    assert "propose_order" in resolved.registry
    assert not resolved.web_search  # no web_search in defaults without config


def test_default_risk_is_readonly():
    agents = AgentSettings()
    resolved = resolve_tools_for(agents, "risk")
    assert set(resolved.registry) == set(READ_ONLY_TOOLS)
    assert "propose_order" not in resolved.registry


def test_default_scoring_is_trimmed():
    agents = AgentSettings()
    resolved = resolve_tools_for(agents, "scoring")
    assert "propose_order" not in resolved.registry
    assert "get_quote" not in resolved.registry
    assert "read_journal" in resolved.registry
    assert "read_memory" in resolved.registry


def test_propose_order_stripped_from_non_strategy_even_if_configured():
    agents = AgentSettings(tools={
        "risk": ["get_quote", "propose_order", "get_bars"],
    })
    resolved = resolve_tools_for(agents, "risk")
    assert "propose_order" not in resolved.registry
    assert "get_quote" in resolved.registry


def test_same_as_dict_entry():
    agents = AgentSettings(tools={
        "risk": ["get_quote", "get_bars"],
        "redteam": [{"same_as": "risk"}],
    })
    r = resolve_tools_for(agents, "risk")
    rt = resolve_tools_for(agents, "redteam")
    assert r.registry == rt.registry


def test_same_as_string_form():
    agents = AgentSettings(tools={
        "risk": ["get_calendar"],
        "redteam": ["same_as:risk"],
    })
    assert resolve_tools_for(agents, "redteam").registry == ("get_calendar",)


def test_unknown_tool_fails_fast():
    agents = AgentSettings(tools={"strategy": ["not_a_real_tool"]})
    try:
        resolve_tools_for(agents, "strategy")
        assert False, "should raise"
    except ValueError as e:
        assert "unknown tools" in str(e)


def test_circular_same_as_fails():
    agents = AgentSettings(tools={
        "risk": [{"same_as": "redteam"}],
        "redteam": [{"same_as": "risk"}],
    })
    try:
        resolve_tools_for(agents, "risk")
        assert False, "should raise"
    except ValueError as e:
        assert "circular" in str(e)


def test_web_search_pseudo_tool():
    agents = AgentSettings(
        tools={"intel": ["web_search"], "strategy": ["all", "web_search"]},
        web_search_max_uses={"intel": 5, "strategy": 3},
    )
    intel = resolve_tools_for(agents, "intel")
    assert intel.registry == ()
    assert intel.web_search is True
    assert intel.web_search_max_uses == 5

    strat = resolve_tools_for(agents, "strategy")
    assert WEB_SEARCH not in strat.registry
    assert strat.web_search is True
    assert strat.web_search_max_uses == 3
    assert "propose_order" in strat.registry


def test_web_search_disabled_when_max_uses_zero():
    agents = AgentSettings(
        tools={"risk": ["get_quote", "web_search"]},
        web_search_max_uses={"risk": 0},
    )
    resolved = resolve_tools_for(agents, "risk")
    assert resolved.web_search is False


def test_web_search_schema():
    schema = web_search_tool_schema(3)
    assert schema["type"] == "web_search_20250305"
    assert schema["name"] == "web_search"
    assert schema["max_uses"] == 3


def test_settings_yaml_tools_load():
    """Production settings.yaml must resolve for every known role."""
    from trading.config import load_config
    config = load_config()
    for role in ("strategy", "risk", "redteam", "scoring", "intel"):
        resolved = config.settings.agents.tools_for(role)
        assert resolved.role == role
        for name in resolved.registry:
            assert name in TOOL_SCHEMAS
    strat = config.settings.agents.tools_for("strategy")
    assert strat.web_search is True
    assert "propose_order" in strat.registry
    assert "scan_universe" in strat.registry
    risk = config.settings.agents.tools_for("risk")
    assert "propose_order" not in risk.registry
    assert "scan_universe" in risk.registry
    intel = config.settings.agents.tools_for("intel")
    assert intel.web_search is True
    assert intel.registry == ()


def test_make_config_tools_for_still_works():
    config = make_config()
    resolved = config.settings.agents.tools_for("strategy")
    assert "propose_order" in resolved.registry

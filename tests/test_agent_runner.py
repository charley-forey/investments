from conftest import make_config

from stubs import (
    ScriptedClient, StubBroker, _Response, make_account, text_block, tool_use_block,
)

from trading.agents.runner import run_agent
from trading.data.journal import Journal
from trading.tools.registry import STRATEGY_TOOLS, ToolContext, ToolRegistry


def build(tmp_path, responses):
    config = make_config()
    journal = Journal(tmp_path / "j.db")
    broker = StubBroker(make_account())
    ctx = ToolContext(config=config, journal=journal, broker=broker,
                      account_state=make_account(), agent_name="strategy")
    reg = ToolRegistry(ctx, STRATEGY_TOOLS)
    client = ScriptedClient(responses)
    return config, journal, reg, client


def test_loop_executes_tool_then_ends(tmp_path):
    responses = [
        _Response([tool_use_block("t1", "get_account_state", {})], "tool_use"),
        _Response([text_block("no trades today")], "end_turn"),
    ]
    config, journal, reg, client = build(tmp_path, responses)
    result = run_agent(
        client, model="m", max_tokens=100, system_prompt="sys", registry=reg,
        user_message="scan", max_iterations=10, journal=journal, agent_name="strategy",
    )
    assert result.stop_reason == "end_turn"
    assert result.final_text == "no trades today"
    assert result.drafts == []
    assert client.calls == 2


def test_loop_captures_draft_from_propose(tmp_path):
    responses = [
        _Response([tool_use_block("t1", "propose_order", {
            "symbol": "AAPL", "asset_class": "stock", "side": "buy", "qty": 10,
            "limit_price": 180.0, "stop_price": 175.0, "thesis": "breakout",
            "expected_edge_usd": 60.0, "strategy_tag": "news-breakout",
        })], "tool_use"),
        _Response([text_block("proposed AAPL")], "end_turn"),
    ]
    config, journal, reg, client = build(tmp_path, responses)
    result = run_agent(
        client, model="m", max_tokens=100, system_prompt="sys", registry=reg,
        user_message="scan", max_iterations=10, journal=journal,
    )
    assert len(result.drafts) == 1
    assert result.drafts[0].symbol == "AAPL"
    assert result.drafts[0].strategy_tag == "news-breakout"


def test_loop_respects_iteration_cap(tmp_path):
    # Always returns tool_use -> loop must stop at the cap.
    responses = [
        _Response([tool_use_block(f"t{i}", "get_account_state", {})], "tool_use")
        for i in range(20)
    ]
    config, journal, reg, client = build(tmp_path, responses)
    result = run_agent(
        client, model="m", max_tokens=100, system_prompt="sys", registry=reg,
        user_message="scan", max_iterations=3, journal=journal,
    )
    assert result.iterations == 3
    assert result.stop_reason == "max_iterations"

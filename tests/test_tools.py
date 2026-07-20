from conftest import make_config

from stubs import StubBroker, make_account

from trading.data.journal import Journal
from trading.tools.registry import (
    READ_ONLY_TOOLS, STRATEGY_TOOLS, TOOL_SCHEMAS, ToolContext, ToolRegistry,
)


def make_ctx(tmp_path, agent="strategy"):
    config = make_config()
    journal = Journal(tmp_path / "j.db")
    broker = StubBroker(make_account())
    ctx = ToolContext(config=config, journal=journal, broker=broker,
                      account_state=make_account(), agent_name=agent)
    return ctx


def test_all_schemas_have_required_fields():
    for name, schema in TOOL_SCHEMAS.items():
        assert "description" in schema
        assert schema["input_schema"]["type"] == "object"


def test_read_only_excludes_propose():
    assert "propose_order" not in READ_ONLY_TOOLS
    assert "propose_order" in STRATEGY_TOOLS


def test_registry_rejects_unknown_tool(tmp_path):
    ctx = make_ctx(tmp_path)
    try:
        ToolRegistry(ctx, ["nonexistent_tool"])
        assert False, "should have raised"
    except ValueError:
        pass


def test_risk_registry_cannot_propose(tmp_path):
    ctx = make_ctx(tmp_path, agent="risk")
    reg = ToolRegistry(ctx, READ_ONLY_TOOLS)
    out = reg.dispatch("propose_order", {"symbol": "AAPL"})
    assert out.startswith("error:")
    assert ctx.drafts == []


def test_account_state_tool_summary(tmp_path):
    ctx = make_ctx(tmp_path)
    reg = ToolRegistry(ctx, STRATEGY_TOOLS)
    out = reg.dispatch("get_account_state", {})
    assert "equity=" in out


def test_propose_registers_draft_without_executing(tmp_path):
    ctx = make_ctx(tmp_path)
    reg = ToolRegistry(ctx, STRATEGY_TOOLS)
    out = reg.dispatch("propose_order", {
        "symbol": "AAPL", "asset_class": "stock", "side": "buy", "qty": 10,
        "limit_price": 180.0, "stop_price": 175.0, "thesis": "test",
        "expected_edge_usd": 50.0, "strategy_tag": "test-tag",
    })
    assert "draft #1" in out
    assert len(ctx.drafts) == 1
    # No order was created in the journal.
    assert ctx.journal.conn.execute("SELECT COUNT(*) AS n FROM orders").fetchone()["n"] == 0


def test_propose_validates_opening_stock_needs_stop(tmp_path):
    ctx = make_ctx(tmp_path)
    reg = ToolRegistry(ctx, STRATEGY_TOOLS)
    out = reg.dispatch("propose_order", {
        "symbol": "AAPL", "asset_class": "stock", "side": "buy", "qty": 10,
        "limit_price": 180.0, "thesis": "no stop", "expected_edge_usd": 50.0,
        "strategy_tag": "t",
    })
    assert out.startswith("error:")
    assert ctx.drafts == []


def test_propose_respects_budget(tmp_path):
    ctx = make_ctx(tmp_path)
    ctx.config.settings.agents.max_proposals_per_cycle = 1
    reg = ToolRegistry(ctx, STRATEGY_TOOLS)
    args = {"symbol": "AAPL", "asset_class": "stock", "side": "buy", "qty": 5,
            "limit_price": 180.0, "stop_price": 175.0, "thesis": "t",
            "expected_edge_usd": 50.0, "strategy_tag": "t"}
    assert "draft #1" in reg.dispatch("propose_order", dict(args))
    assert reg.dispatch("propose_order", dict(args)).startswith("error:")
    assert len(ctx.drafts) == 1


def test_propose_rejects_oversized_with_max_qty(tmp_path):
    ctx = make_ctx(tmp_path)  # equity $50k, position cap $5k
    reg = ToolRegistry(ctx, STRATEGY_TOOLS)
    out = reg.dispatch("propose_order", {
        "symbol": "META", "asset_class": "stock", "side": "buy", "qty": 40,
        "limit_price": 650.0, "stop_price": 630.0, "thesis": "oversized",
        "expected_edge_usd": 200.0, "strategy_tag": "rel-strength",
    })
    assert out.startswith("error:")
    assert "max qty" in out
    assert "Re-propose" in out
    assert ctx.drafts == []


def test_propose_rejects_excessive_risk(tmp_path):
    ctx = make_ctx(tmp_path)  # 1% of $50k = $500 risk budget
    reg = ToolRegistry(ctx, STRATEGY_TOOLS)
    # 20 shares * $30 stop distance = $600 risk > $500, but notional $3600 < $5k
    out = reg.dispatch("propose_order", {
        "symbol": "AAPL", "asset_class": "stock", "side": "buy", "qty": 20,
        "limit_price": 180.0, "stop_price": 150.0, "thesis": "wide stop",
        "expected_edge_usd": 100.0, "strategy_tag": "t",
    })
    assert out.startswith("error:")
    assert "risk" in out.lower()
    assert ctx.drafts == []


def test_get_quote_warns_on_wide_spread(tmp_path):
    from trading.broker.models import Quote
    ctx = make_ctx(tmp_path)
    ctx.broker._quotes["WIDE"] = Quote(symbol="WIDE", bid=100.0, ask=105.0)
    reg = ToolRegistry(ctx, STRATEGY_TOOLS)
    out = reg.dispatch("get_quote", {"symbol": "WIDE"})
    assert "WARNING" in out
    assert "spread" in out.lower()


def test_schemas_returns_only_allowed(tmp_path):
    ctx = make_ctx(tmp_path)
    reg = ToolRegistry(ctx, READ_ONLY_TOOLS)
    names = {s["name"] for s in reg.schemas()}
    assert names == set(READ_ONLY_TOOLS)

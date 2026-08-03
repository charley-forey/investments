"""Regression for the arming blackout: 2026-07-29 .. 2026-08-03.

`armed_plans` held ZERO rows for its entire existence. Everything downstream of
the model worked -- test_armed_plans.py covers the crossing, the debounce, the
single-claim guarantee, the guardrail replay -- because every one of those tests
builds an `OrderProposal(**d)` by hand.

Nothing tested the seam the agent actually goes through. `_t_propose_order`
constructed OrderProposal without forwarding arm_level/arm_direction, so the
three fields the tool schema advertises were dropped on the floor. The agent had
been arming correctly: on 2026-08-03 alone it sent arm_level 16 times
(META 594.7 above, NET 294 above, ...). Every one came out the far side as an
ordinary marketable limit, which is why the risk agent kept vetoing proposals for
"a buy limit that would fill immediately rather than wait for the breakout" (#86)
-- it was reviewing an order that no longer expressed the thesis it was sent.

The cost consequence was the same bug: triggers.py skips the LLM for a symbol
that is already armed, so with arming broken the trigger-hit gate re-woke a full
session on the same four levels every minute -- 293 wakes, $10.21, 89% of the
day's spend.
"""

from conftest import make_config
from stubs import StubBroker, make_account

from trading.data.journal import Journal
from trading.tools.registry import STRATEGY_TOOLS, ToolContext, ToolRegistry


def _registry(tmp_path):
    ctx = ToolContext(config=make_config(), journal=Journal(tmp_path / "j.db"),
                      broker=StubBroker(make_account()), account_state=make_account(),
                      agent_name="strategy")
    return ToolRegistry(ctx, STRATEGY_TOOLS), ctx


def _meta_arm(**over):
    """The exact tool input the agent sent for META at 14:19:45Z on 2026-08-03."""
    # qty 8, not the live 25: the test config caps notional at $5k.
    base = dict(symbol="META", asset_class="stock", side="buy", qty=8,
                order_type="limit", limit_price=594.70, stop_price=586.75,
                target_price=614.60, strategy_tag="breakout", confidence=0.55,
                expected_edge_usd=48.0, thesis="arm only on a fresh break above today's high",
                arm_level=594.70, arm_direction="above")
    base.update(over)
    return base


def test_arm_fields_survive_the_tool_call(tmp_path):
    """The whole bug in one assertion."""
    reg, ctx = _registry(tmp_path)
    out = reg.dispatch("propose_order", _meta_arm())
    assert "error" not in out.lower(), out
    draft = ctx.drafts[-1]
    assert draft.arm_level == 594.70
    assert draft.arm_direction == "above"
    assert draft.is_armed_plan, "agent armed it; the draft must know it is armed"


def test_arm_valid_hours_is_forwarded_when_given(tmp_path):
    reg, ctx = _registry(tmp_path)
    reg.dispatch("propose_order", _meta_arm(arm_valid_hours=3.0))
    assert ctx.drafts[-1].arm_valid_hours == 3.0


def test_omitting_arm_valid_hours_keeps_the_model_default(tmp_path):
    """Passing None explicitly would fail validation (gt=0); absent must mean default."""
    reg, ctx = _registry(tmp_path)
    reg.dispatch("propose_order", _meta_arm())
    assert ctx.drafts[-1].arm_valid_hours == 8.0


def test_an_ordinary_proposal_is_still_not_armed(tmp_path):
    """No arm_level -> a normal immediate order, unchanged behaviour."""
    reg, ctx = _registry(tmp_path)
    inp = _meta_arm()
    del inp["arm_level"], inp["arm_direction"]
    reg.dispatch("propose_order", inp)
    assert not ctx.drafts[-1].is_armed_plan


def test_level_without_direction_is_not_armed(tmp_path):
    """Half an arm instruction must not become a live plan."""
    reg, ctx = _registry(tmp_path)
    inp = _meta_arm()
    del inp["arm_direction"]
    reg.dispatch("propose_order", inp)
    assert not ctx.drafts[-1].is_armed_plan

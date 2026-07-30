"""Risk agent: independent structured review of a single draft proposal.

Uses output_config.format (json_schema) so the verdict is machine-readable. The
risk agent gets read-only tools only — it can investigate but cannot propose.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from ..broker.models import AccountState
from ..config import Config
from ..cost import Usage, supports_adaptive_thinking, usage_from_response
from ..data.journal import Journal
from ..guardrails.models import OrderProposal
from ..tools.assignment import web_search_tool_schema
from ..tools.registry import ToolContext, ToolRegistry
from . import prompts
from .runner import _roll_cache_breakpoint

VERDICT_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["approve", "amend", "veto"]},
        "reason": {"type": "string"},
        "concerns": {"type": "array", "items": {"type": "string"}},
        "qty_mult": {
            "type": ["number", "null"],
            "description": "Only for 'amend': scale position size by this "
                           "(0.25-1.0). Null for approve/veto.",
        },
    },
    "required": ["verdict", "reason", "concerns", "qty_mult"],
    "additionalProperties": False,
}


@dataclass
class RiskVerdict:
    verdict: str            # approve | amend | veto
    reason: str
    concerns: list[str]
    usage: Usage = None     # tokens spent reaching this verdict (billed, so metered)
    qty_mult: float | None = None   # 'amend' only: size scale factor

    def __post_init__(self):
        if self.usage is None:
            self.usage = Usage()

    @property
    def allows_trade(self) -> bool:
        return self.verdict in ("approve", "amend")

    def scaled(self, proposal):
        """The proposal this verdict permits: unchanged for approve, size-scaled
        for amend. Clamped to 0.25-1.0 so an amend can only shrink, never grow."""
        if self.verdict != "amend":
            return proposal
        mult = min(1.0, max(0.25, float(self.qty_mult or 0.5)))
        out = proposal.model_copy(deep=True)
        if out.legs:
            for leg in out.legs:
                leg.qty = max(1, int(leg.qty * mult))
        else:
            out.qty = max(1.0, float(int(proposal.qty * mult)))
        return out


def _limits_summary(config: Config) -> str:
    lim = config.limits
    return (
        f"Guardrail limits (enforced mechanically after you): "
        f"max {lim.position.max_position_pct}% equity / ${lim.position.max_position_usd:,.0f} "
        f"per position; max {lim.orders.max_new_trades_per_day} trades/day; "
        f"risk {lim.position.risk_per_trade_pct}%/trade; "
        f"daily-loss kill switch at {lim.loss_kill_switch.max_daily_loss_pct}%; "
        f"options defined-risk only, max loss ${lim.options.max_loss_per_trade_usd:,.0f}; "
        f"cost hurdle {lim.cost_hurdle.min_edge_multiple}x estimated cost."
    )


def _proposal_summary(p: OrderProposal) -> str:
    # Options carry their size on the legs, so the top-level qty is 0 by design.
    # Rendering that bare "qty=0" read as a non-order and got every option vetoed.
    size = (f"contracts={max((leg.qty for leg in p.legs), default=0)} (size is per-leg; "
            f"top-level qty is unused for options)" if p.is_option else f"qty={p.qty:g}")
    lines = [
        f"symbol={p.symbol} class={p.asset_class} side={p.side} {size} "
        f"strategy={p.strategy_tag}",
        f"limit={p.limit_price} stop={p.stop_price} "
        f"expected_edge_usd={p.expected_edge_usd} confidence={p.confidence}",
        f"thesis: {p.thesis}",
    ]
    for leg in p.legs:
        lines.append(f"  leg: {leg.side} {leg.qty} {leg.right} {leg.strike} exp {leg.expiry} "
                     f"@~{leg.est_premium}")
    return "\n".join(lines)


def review_proposal(
    client,
    config: Config,
    journal: Journal,
    broker,
    account: AccountState,
    proposal: OrderProposal,
    *,
    system_prompt: str | None = None,
    model: str | None = None,
    agent_name: str = "risk",
) -> RiskVerdict:
    resolved = config.settings.agents.tools_for(agent_name)
    ctx = ToolContext(
        config=config, journal=journal, broker=broker,
        account_state=account, agent_name=agent_name,
    )
    registry = ToolRegistry(ctx, list(resolved.registry))

    user_message = (
        "Review this proposal and return your verdict in the required JSON format.\n\n"
        f"{_proposal_summary(proposal)}\n\n"
        f"Account: equity ${account.equity:,.0f}, open positions "
        f"{account.open_position_count}, daily P&L {account.daily_pl_pct:+.2f}%.\n"
        f"{_limits_summary(config)}\n\n"
        "Use read-only tools if you need more context, then respond with JSON."
    )

    # Manual tool loop, then a final constrained-output call for the verdict.
    system = [{"type": "text", "text": system_prompt or prompts.RISK_SYSTEM,
               "cache_control": {"type": "ephemeral", "ttl": "1h"}}]
    review_model = model or config.settings.agents.model_for("risk")
    messages: list[dict] = [{"role": "user", "content": user_message}]
    spent = Usage()
    tools = list(registry.schemas())
    if resolved.web_search:
        tools.append(web_search_tool_schema(resolved.web_search_max_uses))

    for _ in range(config.settings.agents.max_tool_iterations):
        create_kwargs: dict = dict(
            model=review_model,
            max_tokens=config.settings.agents.max_tokens,
            system=system,
            messages=messages,
        )
        if supports_adaptive_thinking(review_model):
            create_kwargs["thinking"] = {"type": "adaptive"}
        effort = config.settings.agents.effort_for(agent_name)
        if effort:
            create_kwargs["output_config"] = {"effort": effort}
        if tools:
            create_kwargs["tools"] = tools
        response = client.messages.create(**create_kwargs)
        spent.add(usage_from_response(response))
        # Counted here rather than in the dispatch branch below so searches on a
        # turn that ends without tool_use are still billed to us.
        spent.web_searches += sum(
            1 for b in response.content
            if getattr(b, "type", None) == "server_tool_use"
            and getattr(b, "name", "") == "web_search"
        )
        if response.stop_reason == "pause_turn":
            messages.append({"role": "assistant", "content": response.content})
            continue
        if response.stop_reason != "tool_use":
            break
        messages.append({"role": "assistant", "content": response.content})
        results = []
        called = []
        for block in response.content:
            btype = getattr(block, "type", None)
            if btype == "server_tool_use" and getattr(block, "name", "") == "web_search":
                called.append("web_search")
                continue
            if btype != "tool_use":
                continue
            out = registry.dispatch(block.name, block.input or {})
            called.append(block.name)
            results.append({
                "type": "tool_result", "tool_use_id": block.id,
                "content": out, "is_error": out.startswith("error:"),
            })
        if journal is not None and called:
            journal.heartbeat(f"agent:{agent_name}", detail=f"tools: {', '.join(called)}")
        if results:
            messages.append({"role": "user", "content": results})
            # Same fix runner.py already had: without it only the ~470-token
            # system prompt was cached and the whole growing transcript was
            # re-billed at full rate on each of up to 25 iterations, plus again
            # on the final verdict call. Risk sat at 28% cache hit vs strategy's
            # 71%.
            _roll_cache_breakpoint(messages)

    # Constrained final verdict.
    messages.append({
        "role": "user",
        "content": "Now output your final verdict as JSON matching the schema.",
    })
    final = client.messages.create(
        model=review_model,
        max_tokens=2000,
        system=system,
        messages=messages,
        output_config={"format": {"type": "json_schema", "schema": VERDICT_SCHEMA}},
    )
    spent.add(usage_from_response(final))
    text = next((b.text for b in final.content if b.type == "text"), "{}")
    data = json.loads(text)
    return RiskVerdict(
        verdict=data.get("verdict", "veto"),
        reason=data.get("reason", "no reason returned"),
        concerns=data.get("concerns", []),
        usage=spent,
        qty_mult=data.get("qty_mult"),
    )

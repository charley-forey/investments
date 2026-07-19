"""Risk agent: independent structured review of a single draft proposal.

Uses output_config.format (json_schema) so the verdict is machine-readable. The
risk agent gets read-only tools only — it can investigate but cannot propose.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from ..broker.models import AccountState
from ..config import Config
from ..data.journal import Journal
from ..guardrails.models import OrderProposal
from ..tools.registry import READ_ONLY_TOOLS, ToolContext, ToolRegistry
from . import prompts

VERDICT_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["approve", "veto"]},
        "reason": {"type": "string"},
        "concerns": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["verdict", "reason", "concerns"],
    "additionalProperties": False,
}


@dataclass
class RiskVerdict:
    verdict: str            # approve | veto
    reason: str
    concerns: list[str]


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
    lines = [
        f"symbol={p.symbol} class={p.asset_class} side={p.side} qty={p.qty:g} "
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
    ctx = ToolContext(
        config=config, journal=journal, broker=broker,
        account_state=account, agent_name=agent_name,
    )
    registry = ToolRegistry(ctx, READ_ONLY_TOOLS)

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
               "cache_control": {"type": "ephemeral"}}]
    review_model = model or config.settings.agents.model_for("risk")
    messages: list[dict] = [{"role": "user", "content": user_message}]
    tools = registry.schemas()

    for _ in range(config.settings.agents.max_tool_iterations):
        response = client.messages.create(
            model=review_model,
            max_tokens=config.settings.agents.max_tokens,
            thinking={"type": "adaptive"},
            system=system,
            tools=tools,
            messages=messages,
        )
        if response.stop_reason == "pause_turn":
            messages.append({"role": "assistant", "content": response.content})
            continue
        if response.stop_reason != "tool_use":
            break
        messages.append({"role": "assistant", "content": response.content})
        results = []
        for block in response.content:
            if block.type == "tool_use":
                out = registry.dispatch(block.name, block.input or {})
                results.append({
                    "type": "tool_result", "tool_use_id": block.id,
                    "content": out, "is_error": out.startswith("error:"),
                })
        messages.append({"role": "user", "content": results})

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
    text = next((b.text for b in final.content if b.type == "text"), "{}")
    data = json.loads(text)
    return RiskVerdict(
        verdict=data.get("verdict", "veto"),
        reason=data.get("reason", "no reason returned"),
        concerns=data.get("concerns", []),
    )

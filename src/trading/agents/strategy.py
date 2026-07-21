"""Strategy agent session: scans the universe and registers draft proposals."""

from __future__ import annotations

from datetime import datetime, time, timezone

from ..broker.models import AccountState
from ..config import Config
from ..data.journal import Journal
from ..tools.registry import ToolContext, ToolRegistry
from . import prompts
from .runner import AgentResult, run_agent


def _limits_context(config: Config, account: AccountState, journal: Journal) -> str:
    """Dynamic, equity-aware caps for the intraday user message (not the frozen
    system prompt — keeps the prompt cache breakpoint intact)."""
    lim = config.limits
    equity = account.equity
    pos_cap = min(lim.position.max_position_usd,
                  equity * lim.position.max_position_pct / 100.0) if equity > 0 else lim.position.max_position_usd
    risk_usd = equity * lim.position.risk_per_trade_pct / 100.0 if equity > 0 else 0.0
    now = datetime.now(timezone.utc)
    day_start = datetime.combine(now.date(), time.min, tzinfo=timezone.utc)
    trades_today = journal.trades_since(day_start)
    remaining = max(0, lim.orders.max_new_trades_per_day - trades_today)
    return (
        f"Hard limits this cycle (guardrails REJECT oversized proposals — they do "
        f"not resize): position cap ${pos_cap:,.0f} "
        f"(min of ${lim.position.max_position_usd:,.0f} and "
        f"{lim.position.max_position_pct:g}% of ${equity:,.0f} equity); "
        f"risk per trade ${risk_usd:,.0f} ({lim.position.risk_per_trade_pct:g}% equity); "
        f"order notional cap ${lim.orders.max_order_notional_usd:,.0f}; "
        f"cost hurdle {lim.cost_hurdle.min_edge_multiple:g}x estimated friction; "
        f"trades remaining today {remaining}/{lim.orders.max_new_trades_per_day}. "
        f"Size qty so notional and stop-risk clear these caps before proposing."
    )


def _recent_failures_context(journal: Journal, *, limit: int = 8) -> str:
    """Surface recent veto/reject reasons so the agent does not repeat the same
    illegal size or thesis that just failed."""
    rows = journal.conn.execute(
        """SELECT p.id, p.symbol, p.status, p.qty, p.limit_price, p.strategy_tag,
                  v.source, v.rule, v.reason
           FROM proposals p
           JOIN verdicts v ON v.proposal_id = p.id
           WHERE p.status IN ('vetoed', 'rejected')
             AND v.verdict IN ('veto', 'reject')
           ORDER BY p.id DESC, v.id DESC
           LIMIT ?""",
        (limit * 3,),  # extra rows then dedupe by proposal
    ).fetchall()
    if not rows:
        return ""
    seen: set[int] = set()
    lines = [
        "Recent proposal failures — do NOT repeat these mistakes this cycle "
        "(re-size or skip; do not re-propose the same illegal structure):"
    ]
    for r in rows:
        pid = int(r["id"])
        if pid in seen:
            continue
        seen.add(pid)
        rule = f" [{r['rule']}]" if r["rule"] else ""
        notional = ""
        if r["limit_price"] and r["qty"]:
            notional = f" notional~${float(r['qty']) * float(r['limit_price']):,.0f}"
        reason = (r["reason"] or "")[:180].replace("\n", " ")
        lines.append(
            f"- #{pid} {r['symbol']} {r['status']} via {r['source']}{rule}"
            f"{notional}: {reason}"
        )
        if len(seen) >= limit:
            break
    return "\n".join(lines) if len(lines) > 1 else ""


def run_strategy_session(
    client,
    config: Config,
    journal: Journal,
    broker,
    account: AccountState,
    *,
    cycle: str = "intraday",
    extra_context: str = "",
) -> AgentResult:
    resolved = config.settings.agents.tools_for("strategy")
    ctx = ToolContext(
        config=config, journal=journal, broker=broker,
        account_state=account, agent_name="strategy",
    )
    registry = ToolRegistry(ctx, list(resolved.registry))

    universe = ", ".join(config.settings.universe.core)
    if cycle == "premarket":
        user_message = f"{prompts.WATCHLIST_PROMPT}\nUniverse: {universe}"
    elif cycle == "postclose":
        user_message = prompts.EOD_PROMPT
    elif cycle == "weekend":
        user_message = f"{prompts.WEEKEND_RESEARCH_PROMPT}\nUniverse: {universe}"
    else:
        failures = _recent_failures_context(journal)
        user_message = (
            f"Intraday scan. Universe: {universe}. "
            f"You may register at most {config.settings.agents.max_proposals_per_cycle} "
            f"proposals this cycle. Begin by reading memory, journal, and account state. "
            f"FIRST review every open position for exit conditions — hit stop, thesis "
            f"invalidated, profit target reached, or an option nearing expiry — and "
            f"propose closes with reduces_position=true before considering any new entry.\n"
            f"{_limits_context(config, account, journal)}"
        )
        if failures:
            user_message += f"\n\n{failures}"
    if extra_context:
        user_message += f"\n\n{extra_context}"

    return run_agent(
        client,
        model=config.settings.agents.model_for("strategy"),
        max_tokens=config.settings.agents.max_tokens,
        system_prompt=prompts.STRATEGY_SYSTEM,
        registry=registry,
        user_message=user_message,
        max_iterations=config.settings.agents.max_tool_iterations,
        journal=journal,
        agent_name="strategy",
        web_search=resolved.web_search,
        web_search_max_uses=resolved.web_search_max_uses,
    )

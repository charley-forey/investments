"""Strategy agent session: scans the universe and registers draft proposals."""

from __future__ import annotations

from ..broker.models import AccountState
from ..config import Config
from ..data.journal import Journal
from ..tools.registry import STRATEGY_TOOLS, ToolContext, ToolRegistry
from . import prompts
from .runner import AgentResult, run_agent


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
    ctx = ToolContext(
        config=config, journal=journal, broker=broker,
        account_state=account, agent_name="strategy",
    )
    registry = ToolRegistry(ctx, STRATEGY_TOOLS)

    universe = ", ".join(config.settings.universe.core)
    if cycle == "premarket":
        user_message = f"{prompts.WATCHLIST_PROMPT}\nUniverse: {universe}"
    elif cycle == "postclose":
        user_message = prompts.EOD_PROMPT
    elif cycle == "weekend":
        user_message = f"{prompts.WEEKEND_RESEARCH_PROMPT}\nUniverse: {universe}"
    else:
        user_message = (
            f"Intraday scan. Universe: {universe}. "
            f"You may register at most {config.settings.agents.max_proposals_per_cycle} "
            f"proposals this cycle. Begin by reading memory, journal, and account state. "
            f"FIRST review every open position for exit conditions — hit stop, thesis "
            f"invalidated, profit target reached, or an option nearing expiry — and "
            f"propose closes with reduces_position=true before considering any new entry."
        )
    if extra_context:
        user_message += f"\n\n{extra_context}"

    return run_agent(
        client,
        model=config.settings.agents.model,
        max_tokens=config.settings.agents.max_tokens,
        system_prompt=prompts.STRATEGY_SYSTEM,
        registry=registry,
        user_message=user_message,
        max_iterations=config.settings.agents.max_tool_iterations,
        journal=journal,
        agent_name="strategy",
    )

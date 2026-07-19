"""Scoring agent: qualitative EOD lessons appended to memory/lessons.md.

Numeric scoring is deterministic (analytics.scorer). This agent reads the day and
distils durable, deduplicated lessons the strategy agent reads next session.
"""

from __future__ import annotations

from pathlib import Path

from ..broker.models import AccountState
from ..config import Config
from ..data.journal import Journal
from ..tools.registry import READ_ONLY_TOOLS, ToolContext, ToolRegistry
from . import prompts
from .runner import run_agent

MAX_LESSONS = 40  # keep the file small so it stays cheap in cached context


def run_scoring_session(
    client, config: Config, journal: Journal, broker, account: AccountState
) -> list[str]:
    ctx = ToolContext(
        config=config, journal=journal, broker=broker,
        account_state=account, agent_name="scoring",
    )
    registry = ToolRegistry(ctx, READ_ONLY_TOOLS)
    result = run_agent(
        client,
        model=config.settings.agents.model_for("scoring"),
        max_tokens=config.settings.agents.max_tokens,
        system_prompt=prompts.SCORING_SYSTEM,
        registry=registry,
        user_message="Review today's journal and closed trades, then output "
                     "your lessons as '- ' lines.",
        max_iterations=config.settings.agents.max_tool_iterations,
        journal=journal,
        agent_name="scoring",
    )
    lessons = [
        line.strip()[2:].strip()
        for line in result.final_text.splitlines()
        if line.strip().startswith("- ")
    ]
    if lessons:
        append_lessons(config, lessons)
    return lessons


def append_lessons(config: Config, lessons: list[str]) -> None:
    """Append new lessons, skipping near-duplicates, capped at MAX_LESSONS."""
    path = Path(config.settings.paths.memory_dir) / "lessons.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    existing_lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    existing = {ln.strip().lower() for ln in existing_lines}

    added = []
    for lesson in lessons:
        key = ("- " + lesson).strip().lower()
        if key not in existing:
            added.append("- " + lesson)
            existing.add(key)

    if not added:
        return
    header = "# Lessons\n\n" if not existing_lines else ""
    body_lines = [ln for ln in existing_lines if ln.startswith("- ")] + added
    body_lines = body_lines[-MAX_LESSONS:]  # keep the most recent
    path.write_text("# Lessons\n\n" + "\n".join(body_lines) + "\n", encoding="utf-8")

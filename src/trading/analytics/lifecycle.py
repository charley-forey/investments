"""Strategy lifecycle — the capital-allocation state machine.

Stages: candidate -> backtest -> paper -> small-live -> scaled. Promotion and
demotion are pure code against thresholds in limits.lifecycle. The current stage
per tag is stored in kv_state ("stage:<tag>"). The guardrail engine remains the
hard backstop; this only governs which strategies may size up and (in M4) trade
live.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..config import Config
from ..data.journal import Journal
from .stats import StrategyStats, stats_by_tag

STAGES = ["candidate", "backtest", "paper", "small-live", "scaled"]

# Fraction of the normal position cap a strategy at each stage may use.
STAGE_SIZING = {
    "candidate": 0.0,      # not tradeable yet
    "backtest": 0.0,
    "paper": 1.0,          # paper mode: full paper sizing
    "small-live": 0.25,    # live but throttled
    "scaled": 1.0,
}


@dataclass
class StageChange:
    tag: str
    old_stage: str
    new_stage: str
    reason: str


def get_stage(journal: Journal, tag: str) -> str:
    return journal.get_state(f"stage:{tag}", "paper")  # default new tags to paper


def set_stage(journal: Journal, tag: str, stage: str) -> None:
    journal.set_state(f"stage:{tag}", stage)


def sizing_fraction(journal: Journal, tag: str) -> float:
    return STAGE_SIZING.get(get_stage(journal, tag), 0.0)


def promote_after_backtest(
    journal: Journal, tag: str, expectancy: float, *, min_expectancy: float = 0.0
) -> StageChange | None:
    """A candidate/backtest strategy that clears the backtest expectancy bar is
    promoted to `paper` (where it may trade paper capital). No-op otherwise."""
    stage = get_stage(journal, tag)
    if stage in ("candidate", "backtest") and expectancy > min_expectancy:
        set_stage(journal, tag, "paper")
        change = StageChange(tag, stage, "paper",
                             f"backtest expectancy ${expectancy:+.2f} > ${min_expectancy:.2f}")
        journal.heartbeat("lifecycle", detail=f"{tag}: {stage}->paper (backtest)")
        return change
    return None


def _next_stage(stage: str) -> str:
    i = STAGES.index(stage)
    return STAGES[min(i + 1, len(STAGES) - 1)]


def _prev_stage(stage: str) -> str:
    i = STAGES.index(stage)
    return STAGES[max(i - 1, 0)]


def evaluate_tag(
    journal: Journal, config: Config, tag: str, stats: StrategyStats,
    losing_weeks: int = 0,
) -> StageChange | None:
    """Decide one promotion/demotion step for a tag given its stats. Returns the
    change applied, or None if the stage is unchanged."""
    gates = config.limits.lifecycle
    stage = get_stage(journal, tag)

    # Demotion: sustained losing streak, regardless of stage.
    if losing_weeks >= gates.demote_after_losing_weeks and stage != "candidate":
        new = _prev_stage(stage)
        set_stage(journal, tag, new)
        return StageChange(tag, stage, new,
                           f"{losing_weeks} losing weeks >= {gates.demote_after_losing_weeks}")

    # Promotion paper -> small-live: enough trades and positive expectancy.
    if stage == "paper":
        if (stats.trades >= gates.paper_to_live_min_trades
                and stats.expectancy > gates.paper_to_live_min_expectancy):
            set_stage(journal, tag, "small-live")
            return StageChange(tag, stage, "small-live",
                               f"{stats.trades} trades, expectancy "
                               f"${stats.expectancy:+.2f} > "
                               f"${gates.paper_to_live_min_expectancy:.2f}")

    # Promotion small-live -> scaled: keep proving out at 2x the trade bar.
    if stage == "small-live":
        if (stats.trades >= gates.paper_to_live_min_trades * 2
                and stats.after_tax_expectancy > 0):
            set_stage(journal, tag, "scaled")
            return StageChange(tag, stage, "scaled",
                               f"{stats.trades} trades, positive after-tax expectancy")

    return None


def run_lifecycle(journal: Journal, config: Config) -> list[StageChange]:
    """Evaluate every tag with recorded scores; apply and journal each change."""
    changes: list[StageChange] = []
    by_tag = stats_by_tag(journal, config.settings.tax)
    for tag, stats in by_tag.items():
        losing_weeks = int(journal.get_state(f"losing_weeks:{tag}", "0"))
        change = evaluate_tag(journal, config, tag, stats, losing_weeks)
        if change:
            changes.append(change)
            journal.heartbeat(
                "lifecycle", status="ok",
                detail=f"{change.tag}: {change.old_stage} -> {change.new_stage} "
                       f"({change.reason})",
            )
    return changes


def stages_summary(journal: Journal) -> str:
    tags = journal.distinct_strategy_tags()
    if not tags:
        return "no strategies tracked yet"
    return "; ".join(f"{t}={get_stage(journal, t)}" for t in sorted(tags))

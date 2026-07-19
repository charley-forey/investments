"""Graduated live-scaling ladder — the one place live risk ceilings are raised, and
only by an explicit, code-enforced human decision.

The approved level is stored in kv_state and applied as a multiplier to live
position sizing (default level 0 -> 1.0x, i.e. no change). Raising the level
requires a human command AND passing the eligibility check (track record). Nothing
the LLM does can raise it. Demotion is always allowed.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..data.journal import Journal
from .stats import stats_by_tag

# level -> live sizing multiplier. Level 0 is the safe default (no scaling).
LADDER = {0: 1.0, 1: 1.5, 2: 2.0, 3: 3.0}

# Eligibility to reach a level: (min total scored trades, min positive after-tax tags).
ELIGIBILITY = {
    1: (30, 1),
    2: (100, 2),
    3: (250, 3),
}


def current_level(journal: Journal) -> int:
    return int(journal.get_state("live_scale_level", "0") or 0)


def multiplier(journal: Journal) -> float:
    return LADDER.get(current_level(journal), 1.0)


@dataclass
class Eligibility:
    level: int
    eligible: bool
    reason: str


def check_eligibility(journal: Journal, rates, target_level: int) -> Eligibility:
    if target_level not in LADDER:
        return Eligibility(target_level, False, f"no such level {target_level}")
    if target_level == 0:
        return Eligibility(0, True, "base level")
    need_trades, need_tags = ELIGIBILITY[target_level]
    by_tag = stats_by_tag(journal, rates)
    total_trades = sum(s.trades for s in by_tag.values())
    positive_tags = sum(1 for s in by_tag.values() if s.after_tax_expectancy > 0)
    if total_trades < need_trades:
        return Eligibility(target_level, False,
                           f"{total_trades}/{need_trades} scored trades")
    if positive_tags < need_tags:
        return Eligibility(target_level, False,
                           f"{positive_tags}/{need_tags} profitable strategies")
    return Eligibility(target_level, True,
                       f"{total_trades} trades, {positive_tags} profitable strategies")


def approve_level(journal: Journal, rates, target_level: int) -> Eligibility:
    """Human action: raise the approved live-scaling level if eligible. Refuses to
    raise beyond what the track record supports; lowering is always allowed."""
    if target_level <= current_level(journal):
        journal.set_state("live_scale_level", str(max(0, target_level)))
        journal.heartbeat("scaling", detail=f"level set to {target_level} (demote/hold)")
        return Eligibility(target_level, True, "demotion/hold applied")
    elig = check_eligibility(journal, rates, target_level)
    if elig.eligible:
        journal.set_state("live_scale_level", str(target_level))
        journal.heartbeat("scaling", detail=f"level raised to {target_level}")
    return elig


def status(journal: Journal) -> str:
    lvl = current_level(journal)
    return f"live-scaling level {lvl} (x{LADDER.get(lvl, 1.0)} live sizing)"

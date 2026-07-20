"""Deterministic EOD scorer + weekly rollup.

The scorer turns closed tax lots into rows in the `scores` table (the input to
all strategy statistics). It is pure and idempotent: each closed lot is scored
once (`scored` flag). The qualitative LLM scoring/lessons run separately in the
scoring agent — numbers here, judgment there.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from ..config import Config
from ..data.journal import Journal
from .lifecycle import StageChange, run_lifecycle
from .stats import stats_by_tag


@dataclass
class ScoreReport:
    scored: int = 0
    gross_pnl: float = 0.0


def score_closed_trades(journal: Journal) -> ScoreReport:
    """Record a score row for every closed-but-unscored tax lot."""
    report = ScoreReport()
    for lot in journal.unscored_closed_lots():
        pnl = float(lot.get("realized_pnl") or 0.0)
        term = lot.get("term") or "short"
        tag = lot.get("strategy_tag") or "untagged"
        proposal_id = lot.get("proposal_id")
        journal.record_score(
            strategy_tag=tag, pnl_usd=pnl, term=term,
            proposal_id=int(proposal_id) if proposal_id is not None else None,
            grade="win" if pnl > 0 else ("loss" if pnl < 0 else "scratch"),
            notes=f"{lot['symbol']} lot#{lot['id']}",
        )
        journal.mark_lot_scored(lot["id"])
        report.scored += 1
        report.gross_pnl += pnl
    if report.scored:
        journal.heartbeat("scorer", status="ok",
                          detail=f"scored {report.scored} trades, "
                                 f"gross ${report.gross_pnl:+.2f}")
    return report


@dataclass
class WeeklyReport:
    changes: list[StageChange]
    tag_pnl: dict[str, float]


def run_weekly(journal: Journal, config: Config) -> WeeklyReport:
    """Weekly rollup: update per-tag losing-week counters, then run the lifecycle
    state machine. Call after the week's trades have been scored."""
    week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    tag_pnl: dict[str, float] = {}
    for tag in journal.distinct_strategy_tags():
        rows = [s for s in journal.scores_for_tag(tag) if (s["ts"] or "") >= week_ago]
        wk_pnl = sum(float(s["pnl_usd"] or 0.0) for s in rows)
        tag_pnl[tag] = wk_pnl
        if rows:  # only update the streak in weeks the tag actually traded
            key = f"losing_weeks:{tag}"
            current = int(journal.get_state(key, "0"))
            journal.set_state(key, str(current + 1 if wk_pnl < 0 else 0))

    changes = run_lifecycle(journal, config)
    journal.heartbeat("weekly", status="ok",
                      detail=f"{len(changes)} stage changes")
    return WeeklyReport(changes=changes, tag_pnl=tag_pnl)

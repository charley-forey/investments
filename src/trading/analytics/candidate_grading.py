"""Shadow-grade scanner candidates the agent never proposed.

`snapshot_universe` records one signal_snapshot row per active scanner candidate,
carrying its template + trigger direction + the regime. This module grades each
matured row by the *forward return* that followed — so every deterministic
template accrues a track record, sliced by regime, at zero capital risk. This is
the sample-widening half of the learning loop: the counterfactual grader only
sees ideas that became proposals (~1-2/cycle); this sees the whole funnel.

Pure grading (`grade_candidate`) is separated from I/O (`grade_pending_candidates`)
so the math is unit-testable without a broker or DB.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..data.journal import Journal
from .counterfactuals import _bars_after

DEFAULT_HORIZON_DAYS = 5
DEFAULT_MIN_AGE_DAYS = 1


@dataclass
class GradingReport:
    graded: int = 0
    skipped: int = 0
    right: int = 0
    wrong: int = 0


def _expected_sign(trigger_direction: str | None) -> int:
    """+1 if the candidate bets the price rises (breakout 'above'), -1 if it bets
    the price falls ('below'). Defaults to long when unknown."""
    return -1 if (trigger_direction or "").lower() == "below" else 1


def grade_candidate(row: dict, bars_after: list[dict], *,
                    horizon_days: int = DEFAULT_HORIZON_DAYS) -> dict | None:
    """Grade one candidate snapshot against the bars that followed it.

    `row` is a signal_snapshot dict (needs last, template, trigger_direction).
    `bars_after` is up to `horizon_days` daily bars strictly after the snapshot.
    Returns outcome fields, or None if there isn't enough data to grade."""
    entry = float(row.get("last") or 0)
    if entry <= 0 or not bars_after:
        return None
    fwd_close = float(bars_after[-1]["close"])
    if fwd_close <= 0:
        return None
    forward_return = round((fwd_close - entry) / entry, 5)
    sign = _expected_sign(row.get("trigger_direction"))
    # "right" = price moved in the direction the template bet on (scratch counts
    # as not-right, matching the counterfactual convention).
    direction_right = (forward_return * sign) > 0
    return {
        "snapshot_id": row["id"],
        "symbol": row["symbol"],
        "template": row.get("template"),
        "regime_trend": row.get("regime_trend"),
        "regime_vol": row.get("regime_vol"),
        "horizon_days": horizon_days,
        "entry_price": entry,
        "forward_return": forward_return,
        "direction_right": direction_right,
    }


def grade_pending_candidates(journal: Journal, broker, *,
                             min_age_days: int = DEFAULT_MIN_AGE_DAYS,
                             horizon_days: int = DEFAULT_HORIZON_DAYS) -> GradingReport:
    """Grade every matured, ungraded candidate snapshot. Idempotent (skips rows
    that already have an outcome). Best-effort per row — one bad symbol never
    sinks the batch."""
    report = GradingReport()
    pending = journal.candidates_needing_grade(older_than_days=min_age_days)
    lookback = max(horizon_days + min_age_days + 5, 30)
    for row in pending:
        try:
            df = broker.get_bars(row["symbol"], days=lookback)
        except Exception:
            report.skipped += 1
            continue
        bars = _bars_after(df, row["ts"], horizon_days)
        outcome = grade_candidate(row, bars, horizon_days=horizon_days)
        if outcome is None:
            report.skipped += 1
            continue
        journal.record_candidate_outcome(**outcome)
        report.graded += 1
        report.right += int(outcome["direction_right"])
        report.wrong += int(not outcome["direction_right"])
    if report.graded:
        journal.heartbeat(
            "candidate_grading", status="ok",
            detail=f"graded {report.graded} (right={report.right} "
                   f"wrong={report.wrong} skipped={report.skipped})",
        )
    return report


def template_stats(journal: Journal, *, by_regime: bool = False) -> list[dict]:
    """Per-template (optionally per (template, regime)) forward-return statistics
    from the graded candidate ledger: sample size, mean forward return, and
    directional hit rate. The evidence base Phases 2-3 read to tune and gate."""
    group = "template, regime_trend, regime_vol" if by_regime else "template"
    rows = journal.conn.execute(
        f"SELECT {group}, COUNT(*) AS n, "
        f"AVG(forward_return) AS avg_fwd, "
        f"AVG(direction_right) AS hit_rate "
        f"FROM candidate_outcomes WHERE template IS NOT NULL "
        f"GROUP BY {group} ORDER BY n DESC",
    ).fetchall()
    out = []
    for r in rows:
        d = {"template": r["template"], "n": r["n"],
             "avg_forward_return": round(r["avg_fwd"] or 0.0, 5),
             "hit_rate": round(r["hit_rate"] or 0.0, 3)}
        if by_regime:
            d["regime_trend"] = r["regime_trend"]
            d["regime_vol"] = r["regime_vol"]
        out.append(d)
    return out

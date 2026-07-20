"""Weekly confidence calibration and veto-quality report.

Back-analyzes the analysis: do confidence numbers mean anything, is the risk
agent too strict, and which guardrail is the binding constraint? Deterministic —
fed into the weekend research agent as context.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field

from ..data.journal import Journal


@dataclass
class CalibrationReport:
    n_proposals: int = 0
    n_outcomes: int = 0
    n_scores: int = 0
    confidence_buckets: dict[str, dict] = field(default_factory=dict)
    veto_hit_rate: float | None = None
    veto_n: int = 0
    veto_right: int = 0
    guardrail_reasons: dict[str, int] = field(default_factory=dict)
    status_counts: dict[str, int] = field(default_factory=dict)
    text: str = ""


def _bucket(confidence: float | None) -> str:
    if confidence is None:
        return "unknown"
    if confidence < 0.4:
        return "0.0-0.4"
    if confidence < 0.6:
        return "0.4-0.6"
    if confidence < 0.8:
        return "0.6-0.8"
    return "0.8-1.0"


def build_calibration_report(journal: Journal) -> CalibrationReport:
    report = CalibrationReport()
    proposals = journal.recent_proposals(limit=500)
    report.n_proposals = len(proposals)
    report.status_counts = dict(Counter(p["status"] for p in proposals))

    # Guardrail rejection reason histogram.
    reasons: Counter = Counter()
    for row in journal.conn.execute(
        "SELECT rule FROM verdicts WHERE source='guardrail' AND verdict='reject' "
        "AND rule IS NOT NULL"
    ).fetchall():
        reasons[row["rule"]] += 1
    report.guardrail_reasons = dict(reasons.most_common())

    # Outcome-linked confidence buckets (counterfactual hyp pnl + realized scores).
    outcomes = {
        r["proposal_id"]: r for r in journal.all_proposal_outcomes()
    }
    report.n_outcomes = len(outcomes)
    scores_by_pid: dict[int, list[float]] = defaultdict(list)
    for s in journal.all_scores():
        if s.get("proposal_id") is not None:
            scores_by_pid[int(s["proposal_id"])].append(float(s["pnl_usd"] or 0))
    report.n_scores = sum(len(v) for v in scores_by_pid.values())

    buckets: dict[str, dict] = defaultdict(
        lambda: {"n": 0, "wins": 0, "avg_pnl": 0.0, "_sum": 0.0}
    )
    for p in proposals:
        pid = p["id"]
        pnl = None
        if pid in scores_by_pid:
            pnl = sum(scores_by_pid[pid]) / len(scores_by_pid[pid])
        elif pid in outcomes and outcomes[pid]["hypothetical_pnl"] is not None:
            pnl = float(outcomes[pid]["hypothetical_pnl"])
        if pnl is None:
            continue
        b = _bucket(p.get("confidence"))
        buckets[b]["n"] += 1
        buckets[b]["_sum"] += pnl
        if pnl > 0:
            buckets[b]["wins"] += 1
    for b, d in buckets.items():
        d["avg_pnl"] = round(d["_sum"] / d["n"], 2) if d["n"] else 0.0
        d["win_rate"] = round(d["wins"] / d["n"], 3) if d["n"] else 0.0
        del d["_sum"]
    report.confidence_buckets = dict(sorted(buckets.items()))

    # Risk-agent veto quality from counterfactuals.
    veto_right = veto_n = 0
    for p in proposals:
        if p["status"] != "vetoed":
            continue
        o = outcomes.get(p["id"])
        if o is None or o["verdict_was_right"] is None:
            continue
        veto_n += 1
        if o["verdict_was_right"]:
            veto_right += 1
    report.veto_n = veto_n
    report.veto_right = veto_right
    report.veto_hit_rate = (veto_right / veto_n) if veto_n else None

    report.text = _format(report)
    return report


def _format(report: CalibrationReport) -> str:
    lines = [
        "# Calibration & veto-quality report",
        "",
        f"Proposals reviewed: {report.n_proposals}",
        f"Status mix: {report.status_counts or '{}'}",
        f"Counterfactual outcomes: {report.n_outcomes}; "
        f"realized scores linked to proposals: {report.n_scores}",
        "",
        "## Confidence calibration (realized + counterfactual PnL)",
    ]
    if report.confidence_buckets:
        for b, d in report.confidence_buckets.items():
            lines.append(
                f"- {b}: n={d['n']} win_rate={d['win_rate']:.0%} "
                f"avg_pnl=${d['avg_pnl']:+.0f}"
            )
    else:
        lines.append("- insufficient linked outcomes yet")

    lines.append("")
    lines.append("## Risk-agent veto hit-rate")
    if report.veto_hit_rate is not None:
        lines.append(
            f"- {report.veto_right}/{report.veto_n} vetoes were correct in hindsight "
            f"({report.veto_hit_rate:.0%}). "
            f"{'Risk agent looks well-calibrated.' if report.veto_hit_rate >= 0.6 else 'Risk agent may be over-vetoing winners — review theses.'}"
        )
    else:
        lines.append("- no graded vetoes yet (need aged vetoed proposals + bars)")

    lines.append("")
    lines.append("## Guardrail rejection reasons")
    if report.guardrail_reasons:
        for rule, n in report.guardrail_reasons.items():
            lines.append(f"- {rule}: {n}")
    else:
        lines.append("- none recorded")
    return "\n".join(lines) + "\n"


def persist_calibration(journal: Journal, report: CalibrationReport,
                        memory_dir: str) -> str:
    """Write the report to memory/calibration.md and kv_state for dashboards."""
    from pathlib import Path

    path = Path(memory_dir) / "calibration.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(report.text, encoding="utf-8")
    journal.set_state("calibration_veto_hit_rate",
                      "" if report.veto_hit_rate is None else f"{report.veto_hit_rate:.3f}")
    journal.set_state("calibration_n_outcomes", str(report.n_outcomes))
    journal.heartbeat("calibration", status="ok",
                      detail=f"outcomes={report.n_outcomes} veto_n={report.veto_n}")
    return str(path)

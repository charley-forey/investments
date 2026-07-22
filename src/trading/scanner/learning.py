"""Scanner learning loop: retune OpportunityScore weights and promote/demote core."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from ..analytics.opportunity import DEFAULT_WEIGHTS
from ..config import Config
from ..data.journal import Journal

log = logging.getLogger("trading.scanner.learning")


@dataclass
class LearningReport:
    weights_updated: bool = False
    promoted: list[str] | None = None
    demoted: list[str] | None = None
    detail: str = ""

    def __post_init__(self):
        if self.promoted is None:
            self.promoted = []
        if self.demoted is None:
            self.demoted = []


def _scores_by_source(journal: Journal) -> dict[str, list[float]]:
    """Map discovery_source -> list of realized pnl from linked scores."""
    rows = journal.conn.execute(
        """SELECT p.discovery_source AS src, s.pnl_usd AS pnl
           FROM scores s
           JOIN proposals p ON p.id = s.proposal_id
           WHERE p.discovery_source IS NOT NULL AND s.pnl_usd IS NOT NULL"""
    ).fetchall()
    out: dict[str, list[float]] = {}
    for r in rows:
        src = r["src"] or "unknown"
        out.setdefault(src, []).append(float(r["pnl"]))
    return out


def _candidate_hit_rate(journal: Journal) -> dict[str, dict]:
    """Per-symbol scanner proposal outcomes (submitted vs vetoed)."""
    rows = journal.conn.execute(
        """SELECT symbol, status, COUNT(*) AS n
           FROM proposals
           WHERE discovery_source = 'scanner'
           GROUP BY symbol, status"""
    ).fetchall()
    out: dict[str, dict] = {}
    for r in rows:
        sym = r["symbol"]
        out.setdefault(sym, {"submitted": 0, "vetoed": 0, "rejected": 0, "other": 0})
        st = r["status"]
        if st in out[sym]:
            out[sym][st] += int(r["n"])
        else:
            out[sym]["other"] += int(r["n"])
    return out


def retune_weights(journal: Journal) -> dict[str, float]:
    """Nudge weights from scanner vs core expectancy. Conservative ±10% caps."""
    by_src = _scores_by_source(journal)
    weights = dict(DEFAULT_WEIGHTS)
    raw = journal.get_state("opportunity_weights")
    if raw:
        try:
            weights.update(json.loads(raw))
        except json.JSONDecodeError:
            pass

    scanner_pnls = by_src.get("scanner") or []
    core_pnls = by_src.get("core") or []
    if len(scanner_pnls) < 5:
        return weights  # not enough data

    avg_s = sum(scanner_pnls) / len(scanner_pnls)
    avg_c = (sum(core_pnls) / len(core_pnls)) if core_pnls else 0.0

    # If scanner underperforms core, dial down day_pct/gap chase; boost rvol/range quality.
    if avg_s < avg_c:
        weights["day_pct"] = max(8.0, weights["day_pct"] * 0.9)
        weights["gap_pct"] = max(6.0, weights["gap_pct"] * 0.9)
        weights["rvol"] = min(30.0, weights["rvol"] * 1.05)
        weights["range_break"] = min(28.0, weights["range_break"] * 1.05)
    elif avg_s > 0:
        weights["rs_vs_spy"] = min(20.0, weights["rs_vs_spy"] * 1.05)
        weights["news_spike"] = min(18.0, weights["news_spike"] * 1.05)

    journal.set_state("opportunity_weights", json.dumps(weights))
    journal.set_state("opportunity_weights_ts", datetime.now(timezone.utc).isoformat())
    return weights


def promote_demote_core(config: Config, journal: Journal, *,
                        min_submitted: int = 3,
                        promote_win_rate: float = 0.55,
                        max_promotions: int = 3) -> LearningReport:
    """Promote frequent scanner winners into runtime core (journal kv);
    demote unused veto magnets. Does NOT rewrite settings.yaml or raise risk limits."""
    hits = _candidate_hit_rate(journal)
    core = {s.upper() for s in config.settings.universe.core}
    promote: list[str] = []
    for sym, counts in hits.items():
        sub = counts.get("submitted", 0)
        veto = counts.get("vetoed", 0)
        if sub < min_submitted:
            continue
        total = sub + veto
        if total <= 0:
            continue
        if sub / total >= promote_win_rate and sym not in core:
            promote.append(sym)
    promote = promote[:max_promotions]

    demote: list[str] = []
    for sym in core:
        if sym in ("SPY", "QQQ", "IWM", "GLD"):
            continue
        c = hits.get(sym)
        if not c:
            continue
        if c.get("submitted", 0) == 0 and c.get("vetoed", 0) >= 5:
            demote.append(sym)

    prev_promo = []
    prev_demo = []
    try:
        prev_promo = json.loads(journal.get_state("promoted_core") or "[]")
        prev_demo = json.loads(journal.get_state("demoted_core") or "[]")
    except json.JSONDecodeError:
        pass
    new_promo = sorted(set(prev_promo) | set(promote) - set(demote))
    new_demo = sorted(set(prev_demo) | set(demote) - set(promote))
    journal.set_state("promoted_core", json.dumps(new_promo))
    journal.set_state("demoted_core", json.dumps(new_demo))
    if not promote and not demote:
        return LearningReport(detail="no membership changes")
    return LearningReport(
        promoted=promote, demoted=demote,
        detail=f"promoted={promote} demoted={demote} "
               f"runtime_promo={new_promo} runtime_demo={new_demo}",
    )


def effective_core_symbols(config: Config, journal: Journal | None = None) -> list[str]:
    """Core ∪ journal promotions − demotions."""
    core = [s.upper() for s in config.settings.universe.core]
    if journal is None:
        return core
    try:
        promo = json.loads(journal.get_state("promoted_core") or "[]")
        demo = set(json.loads(journal.get_state("demoted_core") or "[]"))
    except json.JSONDecodeError:
        return core
    out = []
    seen = set()
    for s in core + [str(x).upper() for x in promo]:
        if s in demo or s in seen:
            continue
        seen.add(s)
        out.append(s)
    return out


def run_scanner_learning(config: Config, journal: Journal) -> LearningReport:
    weights = retune_weights(journal)
    report = promote_demote_core(config, journal)
    report.weights_updated = True
    report.detail = f"weights_ok; {report.detail}; keys={list(weights.keys())}"
    journal.heartbeat("scanner_learning", status="ok", detail=report.detail)
    log.info("scanner learning: %s", report.detail)
    return report


def stats_by_source(journal: Journal) -> str:
    rows = journal.conn.execute(
        """SELECT COALESCE(discovery_source, 'untagged') AS src,
                  status, COUNT(*) AS n
           FROM proposals
           GROUP BY src, status
           ORDER BY src, status"""
    ).fetchall()
    if not rows:
        return "no proposals tagged by discovery source yet"
    lines = ["Proposals by discovery source:"]
    for r in rows:
        lines.append(f"  {r['src']:<10} {r['status']:<16} {r['n']}")
    # Expectancy by source when scores exist
    pnls = journal.conn.execute(
        """SELECT COALESCE(p.discovery_source, 'untagged') AS src,
                  COUNT(*) AS n, ROUND(AVG(s.pnl_usd), 2) AS avg_pnl,
                  ROUND(SUM(s.pnl_usd), 2) AS sum_pnl
           FROM scores s JOIN proposals p ON p.id = s.proposal_id
           GROUP BY src"""
    ).fetchall()
    if pnls:
        lines.append("Scored expectancy by source:")
        for r in pnls:
            lines.append(
                f"  {r['src']:<10} n={r['n']} avg_pnl=${r['avg_pnl']} "
                f"sum=${r['sum_pnl']}"
            )
    return "\n".join(lines)

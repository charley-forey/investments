"""Counterfactual outcomes for vetoed/rejected proposals.

Grades the analysis loop when no trade was taken: replay daily bars after the
proposal timestamp and ask whether the stop would have hit before the target.
This is how the system learns from passes, not just fills.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from ..data.journal import Journal


DEFAULT_HORIZON_DAYS = 5
# Grade a veto the day after it was made. At 5 days nothing was ever eligible, so
# proposal_outcomes stayed empty and the scoring agent wrote lessons from recalled
# P&L instead of arithmetic — and got the sign wrong on one of three names.
DEFAULT_MIN_AGE_DAYS = 1


@dataclass
class CounterfactualReport:
    evaluated: int = 0
    skipped: int = 0
    right: int = 0
    wrong: int = 0


def _bar_date(idx) -> str:
    ts = idx[1] if isinstance(idx, tuple) else idx
    return str(ts)[:10]


def _bars_after(df, proposal_ts: str, horizon_days: int):
    """Return up to `horizon_days` daily bars strictly after the proposal date."""
    if df is None or len(df) == 0:
        return []
    prop_day = proposal_ts[:10]
    rows = []
    for idx, row in df.iterrows():
        d = _bar_date(idx)
        if d <= prop_day:
            continue
        rows.append({
            "date": d,
            "open": float(row["open"]),
            "high": float(row["high"]),
            "low": float(row["low"]),
            "close": float(row["close"]),
        })
        if len(rows) >= horizon_days:
            break
    return rows


def _target_price(proposal: dict, entry: float, stop: float | None,
                  default_r: float = 2.0) -> float | None:
    """Infer a take-profit from expected edge or a default R-multiple."""
    qty = float(proposal.get("qty") or 0)
    edge = proposal.get("expected_edge_usd")
    side = (proposal.get("side") or "buy").lower()
    if qty > 0 and edge is not None and entry > 0:
        per_share = float(edge) / qty
        return entry + per_share if side == "buy" else entry - per_share
    if stop is not None and entry > 0 and default_r > 0:
        risk = abs(entry - stop)
        if risk > 0:
            return entry + default_r * risk if side == "buy" else entry - default_r * risk
    return None


def evaluate_proposal(proposal: dict, bars: list[dict], *,
                      horizon_days: int = DEFAULT_HORIZON_DAYS) -> dict | None:
    """Simulate a long/short hold over `bars`. Returns outcome fields or None if
    we lack enough data to grade."""
    if not bars:
        return None
    entry = float(proposal.get("limit_price") or 0)
    if entry <= 0:
        entry = float(bars[0]["open"])
    stop = proposal.get("stop_price")
    stop = float(stop) if stop is not None else None
    qty = float(proposal.get("qty") or 0)
    if qty <= 0:
        return None
    side = (proposal.get("side") or "buy").lower()
    is_long = side == "buy"
    target = _target_price(proposal, entry, stop)

    mfe = 0.0  # max favorable excursion in $
    mae = 0.0  # max adverse excursion in $
    stop_hit = False
    target_hit = False
    exit_price = bars[-1]["close"]
    notes_parts: list[str] = []

    for bar in bars:
        if is_long:
            fav = (bar["high"] - entry) * qty
            adv = (entry - bar["low"]) * qty
            # Conservative intrabar: adverse (stop) checked before favorable.
            if stop is not None and bar["low"] <= stop:
                stop_hit = True
                exit_price = stop
                notes_parts.append(f"stop hit {bar['date']}")
                break
            if target is not None and bar["high"] >= target:
                target_hit = True
                exit_price = target
                notes_parts.append(f"target hit {bar['date']}")
                break
        else:
            fav = (entry - bar["low"]) * qty
            adv = (bar["high"] - entry) * qty
            if stop is not None and bar["high"] >= stop:
                stop_hit = True
                exit_price = stop
                notes_parts.append(f"stop hit {bar['date']}")
                break
            if target is not None and bar["low"] <= target:
                target_hit = True
                exit_price = target
                notes_parts.append(f"target hit {bar['date']}")
                break
        mfe = max(mfe, fav)
        mae = max(mae, adv)
    else:
        notes_parts.append(f"horizon exit @ {exit_price:.2f} on {bars[-1]['date']}")

    hyp = (exit_price - entry) * qty if is_long else (entry - exit_price) * qty
    # If we exited early, refresh MFE/MAE to include the exit move.
    if is_long:
        mfe = max(mfe, (exit_price - entry) * qty)
        mae = max(mae, (entry - exit_price) * qty)
    else:
        mfe = max(mfe, (entry - exit_price) * qty)
        mae = max(mae, (exit_price - entry) * qty)

    # For a pass (veto/reject): right if the trade would have lost or scratched.
    status = proposal.get("status") or ""
    verdict_right = None
    if status in ("vetoed", "rejected"):
        verdict_right = hyp <= 0

    return {
        "horizon_days": horizon_days,
        "entry_price": entry,
        "stop_price": stop,
        "target_price": target,
        "max_favorable_usd": round(mfe, 2),
        "max_adverse_usd": round(mae, 2),
        "hypothetical_pnl": round(hyp, 2),
        "stop_hit": stop_hit,
        "target_hit": target_hit,
        "verdict_was_right": verdict_right,
        "notes": "; ".join(notes_parts),
    }


def evaluate_pending(journal: Journal, broker, *,
                     min_age_days: int = DEFAULT_MIN_AGE_DAYS,
                     horizon_days: int = DEFAULT_HORIZON_DAYS) -> CounterfactualReport:
    """Score every aged vetoed/rejected proposal that lacks an outcome row."""
    report = CounterfactualReport()
    pending = journal.proposals_needing_outcome(older_than_days=min_age_days)
    lookback = max(horizon_days + min_age_days + 5, 30)
    for prop in pending:
        if (prop.get("asset_class") or "stock") != "stock":
            report.skipped += 1
            continue
        try:
            df = broker.get_bars(prop["symbol"], days=lookback)
        except Exception:
            report.skipped += 1
            continue
        bars = _bars_after(df, prop["ts"], horizon_days)
        outcome = evaluate_proposal(prop, bars, horizon_days=horizon_days)
        if outcome is None:
            report.skipped += 1
            continue
        journal.record_proposal_outcome(proposal_id=prop["id"], **outcome)
        report.evaluated += 1
        if outcome["verdict_was_right"] is True:
            report.right += 1
        elif outcome["verdict_was_right"] is False:
            report.wrong += 1
    if report.evaluated:
        journal.heartbeat(
            "counterfactuals", status="ok",
            detail=(f"evaluated {report.evaluated} "
                    f"(right={report.right} wrong={report.wrong} "
                    f"skipped={report.skipped})"),
        )
    return report


def outcomes_summary(journal: Journal, limit: int = 20) -> str:
    """Compact text block for the scoring / research agents."""
    rows = journal.conn.execute(
        """SELECT o.*, p.symbol, p.side, p.status, p.strategy_tag, p.confidence
           FROM proposal_outcomes o
           JOIN proposals p ON p.id = o.proposal_id
           ORDER BY o.id DESC LIMIT ?""",
        (limit,),
    ).fetchall()
    if not rows:
        return "no counterfactual outcomes yet"
    lines = ["Counterfactual outcomes (vetoed/rejected proposals graded after the fact):"]
    right = sum(1 for r in rows if r["verdict_was_right"] == 1)
    wrong = sum(1 for r in rows if r["verdict_was_right"] == 0)
    lines.append(f"  recent sample: {right} right / {wrong} wrong vetoes")
    for r in rows[:10]:
        flag = ("RIGHT" if r["verdict_was_right"] == 1
                else ("WRONG" if r["verdict_was_right"] == 0 else "?"))
        lines.append(
            f"  #{r['proposal_id']} {r['symbol']} {r['status']} {flag}: "
            f"hyp ${r['hypothetical_pnl']:+.0f} "
            f"(MFE ${r['max_favorable_usd']:+.0f} / MAE ${r['max_adverse_usd']:+.0f})"
        )
    return "\n".join(lines)

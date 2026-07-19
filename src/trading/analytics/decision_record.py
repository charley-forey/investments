"""The unified Decision Record — the single queryable place that ties together
*everything* about one trade decision: the thesis, the agent's captured reasoning
and tool calls, the risk and red-team verdicts, the guardrail outcome, the resulting
order/fill, and the eventual score. This is the historical log of all thinking,
analysis, decisions, and metrics.

Pure read over the journal (proposals, verdicts, reasoning, orders, fills, scores).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from ..data.journal import Journal


@dataclass
class DecisionRecord:
    proposal_id: int
    ts: str
    agent: str
    strategy_tag: str
    symbol: str
    side: str
    qty: float
    asset_class: str
    status: str
    thesis: str | None
    expected_edge_usd: float | None
    confidence: float | None
    reasoning: str = ""
    tool_calls: list = field(default_factory=list)
    verdicts: list = field(default_factory=list)     # (source, verdict, rule, reason)
    orders: list = field(default_factory=list)
    scores: list = field(default_factory=list)

    def summary_line(self) -> str:
        return (f"#{self.proposal_id} {self.ts[:16]} {self.strategy_tag} {self.side} "
                f"{self.qty:g} {self.symbol} -> {self.status}")

    def full_text(self) -> str:
        lines = [self.summary_line(), ""]
        lines.append(f"Thesis: {self.thesis or 'n/a'}")
        lines.append(f"Expected edge: ${self.expected_edge_usd or 0:.2f}  "
                     f"confidence: {self.confidence}")
        if self.reasoning:
            lines += ["", "Agent reasoning (summarized):", self.reasoning]
        if self.tool_calls:
            lines.append("")
            lines.append("Tools consulted: " + ", ".join(
                tc.get("name", "?") for tc in self.tool_calls))
        if self.verdicts:
            lines += ["", "Verdicts:"]
            for v in self.verdicts:
                rule = f"[{v['rule']}] " if v.get("rule") else ""
                lines.append(f"  {v['source']}: {v['verdict']} {rule}{v.get('reason') or ''}")
        if self.orders:
            lines += ["", "Orders:"]
            for o in self.orders:
                lines.append(f"  {o['side']} {o['qty']:g} {o['symbol']} @ {o['limit_price']} "
                             f"({o['status']})")
        if self.scores:
            lines += ["", "Outcome:"]
            for s in self.scores:
                lines.append(f"  grade {s.get('grade')} pnl ${s.get('pnl_usd') or 0:.2f}"
                             + (f" — {s.get('notes')}" if s.get('notes') else ""))
        return "\n".join(lines)


def build_record(journal: Journal, proposal_id: int) -> DecisionRecord | None:
    p = journal.get_proposal(proposal_id)
    if p is None:
        return None
    reasoning_rows = journal.reasoning_for(proposal_id)
    reasoning = "\n\n".join(r["reasoning"] for r in reasoning_rows if r["reasoning"])
    tool_calls = []
    for r in reasoning_rows:
        if r["tool_calls_json"]:
            try:
                tool_calls += json.loads(r["tool_calls_json"])
            except ValueError:
                pass
    orders = [dict(o) for o in journal.conn.execute(
        "SELECT * FROM orders WHERE proposal_id=?", (proposal_id,)).fetchall()]
    scores = [dict(s) for s in journal.conn.execute(
        "SELECT * FROM scores WHERE proposal_id=?", (proposal_id,)).fetchall()]
    return DecisionRecord(
        proposal_id=proposal_id, ts=p["ts"], agent=p["agent"],
        strategy_tag=p["strategy_tag"], symbol=p["symbol"], side=p["side"],
        qty=p["qty"], asset_class=p["asset_class"], status=p["status"],
        thesis=p["thesis"], expected_edge_usd=p["expected_edge_usd"],
        confidence=p["confidence"], reasoning=reasoning, tool_calls=tool_calls,
        verdicts=journal.verdicts_for(proposal_id), orders=orders, scores=scores,
    )


def list_records(journal: Journal, limit: int = 25) -> list[DecisionRecord]:
    rows = journal.conn.execute(
        "SELECT id FROM proposals ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    out = []
    for r in rows:
        rec = build_record(journal, r["id"])
        if rec:
            out.append(rec)
    return out

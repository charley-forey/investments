"""Pure broker/journal position reconciliation.

Compares the broker's live positions against the journal's view (open-lot
quantities) and reports per-symbol drift: quantity mismatches, positions the
broker has that the journal doesn't, and vice-versa. No I/O — callers fetch the
two sides and hand them in, so this is trivially testable and reusable by the
postclose cycle, the dashboard, and ad-hoc checks alike.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Discrepancy:
    symbol: str
    broker_qty: float
    journal_qty: float
    kind: str  # 'qty_mismatch' | 'missing_in_journal' | 'missing_in_broker'

    @property
    def delta(self) -> float:
        return self.broker_qty - self.journal_qty


@dataclass
class ReconcileReport:
    discrepancies: list[Discrepancy] = field(default_factory=list)

    @property
    def is_clean(self) -> bool:
        return not self.discrepancies

    def summary(self) -> str:
        if self.is_clean:
            return "reconciliation clean: broker and journal agree"
        lines = [f"reconciliation: {len(self.discrepancies)} discrepancy(ies)"]
        for d in self.discrepancies:
            lines.append(
                f"  {d.symbol}: broker={d.broker_qty:g} journal={d.journal_qty:g} "
                f"(delta {d.delta:+g}, {d.kind})"
            )
        return "\n".join(lines)


def _as_qty_map(positions) -> dict[str, float]:
    """Coerce broker positions or a journal view into {symbol: qty}. Accepts a
    mapping (symbol -> qty) or an iterable of objects/dicts with symbol+qty.
    Sums duplicate symbols (e.g. multiple open lots). Never raises."""
    out: dict[str, float] = {}
    if positions is None:
        return out
    if isinstance(positions, dict):
        items = positions.items()
    else:
        items = None
    try:
        if items is not None:
            for sym, qty in items:
                _add(out, sym, qty)
        else:
            for p in positions:
                if isinstance(p, dict):
                    _add(out, p.get("symbol"), p.get("qty"))
                else:
                    _add(out, getattr(p, "symbol", None), getattr(p, "qty", None))
    except TypeError:
        return out
    return out


def _add(out: dict[str, float], sym, qty) -> None:
    if sym is None or qty is None:
        return
    try:
        out[str(sym).upper()] = out.get(str(sym).upper(), 0.0) + float(qty)
    except (TypeError, ValueError):
        return


def reconcile(broker_positions, journal_positions, *, tolerance: float = 1e-6) -> ReconcileReport:
    """Diff broker vs. journal positions. Drift within ``tolerance`` shares is
    ignored. Returns a :class:`ReconcileReport`; pure and never raises."""
    broker = _as_qty_map(broker_positions)
    journal = _as_qty_map(journal_positions)
    report = ReconcileReport()
    for symbol in sorted(set(broker) | set(journal)):
        b = broker.get(symbol, 0.0)
        j = journal.get(symbol, 0.0)
        if abs(b - j) <= tolerance:
            continue
        if symbol not in journal:
            kind = "missing_in_journal"
        elif symbol not in broker:
            kind = "missing_in_broker"
        else:
            kind = "qty_mismatch"
        report.discrepancies.append(Discrepancy(symbol, b, j, kind))
    return report

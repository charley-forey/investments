"""Tax accounting: wash-sale cost-basis deferral, realized-gains reporting, CSV
export, and tax-loss-harvesting candidates. Pure computation over the journal's
tax lots — the authoritative internal cost-basis record.

Note on broker lot instructions: Alpaca sets the cost-basis method at the account
level, not per closing order, so we cannot push HIFO lot IDs on each sell via the
API. Our internal HIFO tracking is authoritative; reconcile it against the broker's
year-end 1099 with `realized_gains_report` and resolve differences by setting the
Alpaca account cost-basis method to match (HIFO where supported).
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import datetime, timedelta

from ..data.journal import Journal


@dataclass
class WashAdjustment:
    loss_lot_id: int
    replacement_lot_id: int
    symbol: str
    disallowed_usd: float
    shares_matched: float


def _dt(iso: str) -> datetime:
    return datetime.fromisoformat(iso)


def apply_wash_sale_adjustments(journal: Journal, window_days: int = 30) -> list[WashAdjustment]:
    """Detect wash sales and defer the disallowed loss into the replacement lot's
    basis (raising its cost) and holding period (backdating its open date). Marks
    the loss lot so it isn't re-processed. Idempotent across runs.

    Matching is per security (ticker for stock, OCC symbol for options) and
    one replacement lot per loss lot — a documented simplification of the full IRS
    rule, sufficient because our lots are already split to fill quantities.
    """
    adjustments: list[WashAdjustment] = []
    for loss in journal.loss_lots_needing_wash_check():
        close_dt = _dt(loss["close_ts"])
        lo = (close_dt - timedelta(days=window_days)).isoformat()
        hi = (close_dt + timedelta(days=window_days)).isoformat()
        candidates = journal.lots_opened_between(
            loss["symbol"], lo, hi, exclude_id=loss["id"]
        )
        replacement = next((c for c in candidates if not c["wash_adjusted"]), None)
        if replacement is None:
            continue

        loss_qty = loss["qty"]
        shares_matched = min(loss_qty, replacement["qty"])
        disallowed = abs(loss["realized_pnl"]) * (shares_matched / loss_qty)

        # Raise replacement basis by the disallowed dollars, spread per unit.
        mult = (replacement["multiplier"] or 1.0)
        per_unit = disallowed / (replacement["qty"] * mult)
        new_open_price = replacement["open_price"] + per_unit

        # Extend holding period: backdate the replacement's open date by the loss
        # lot's holding period.
        loss_holding = (close_dt - _dt(loss["open_ts"])).days
        new_open_ts = (_dt(replacement["open_ts"]) - timedelta(days=loss_holding)).isoformat()

        journal.apply_basis_adjustment(
            replacement["id"], new_open_price=new_open_price, new_open_ts=new_open_ts
        )
        journal.flag_wash_sale(loss["id"], disallowed)
        adjustments.append(WashAdjustment(
            loss_lot_id=loss["id"], replacement_lot_id=replacement["id"],
            symbol=loss["symbol"], disallowed_usd=round(disallowed, 2),
            shares_matched=shares_matched,
        ))
    if adjustments:
        journal.heartbeat("wash_sale", detail=f"{len(adjustments)} adjustment(s)")
    return adjustments


@dataclass
class RealizedRow:
    symbol: str
    qty: float
    open_ts: str
    close_ts: str
    proceeds: float
    basis: float
    realized_pnl: float        # raw gain/loss before wash-sale disallowance
    wash_disallowed: float     # loss disallowed this lot (>= 0)
    allowed_pnl: float         # realized_pnl + wash_disallowed (what's taxable now)
    term: str


def realized_gains_report(journal: Journal, year: int | None = None) -> list[RealizedRow]:
    rows: list[RealizedRow] = []
    for lot in journal.all_closed_lots():
        if year is not None and _dt(lot["close_ts"]).year != year:
            continue
        mult = lot["multiplier"] or 1.0
        proceeds = (lot["close_price"] or 0.0) * lot["qty"] * mult
        basis = lot["open_price"] * lot["qty"] * mult
        realized = lot["realized_pnl"] or 0.0
        disallowed = lot["wash_disallowed"] or 0.0
        rows.append(RealizedRow(
            symbol=lot["symbol"], qty=lot["qty"], open_ts=lot["open_ts"],
            close_ts=lot["close_ts"], proceeds=round(proceeds, 2), basis=round(basis, 2),
            realized_pnl=round(realized, 2), wash_disallowed=round(disallowed, 2),
            allowed_pnl=round(realized + disallowed, 2),
            term=lot["term"] or "short",
        ))
    return rows


def realized_totals(rows: list[RealizedRow]) -> dict:
    short = sum(r.allowed_pnl for r in rows if r.term == "short")
    long = sum(r.allowed_pnl for r in rows if r.term == "long")
    disallowed = sum(r.wash_disallowed for r in rows)
    return {
        "short_term": round(short, 2),
        "long_term": round(long, 2),
        "total": round(short + long, 2),
        "wash_disallowed": round(disallowed, 2),
        "trades": len(rows),
    }


def export_realized_gains_csv(journal: Journal, path: str, year: int | None = None) -> int:
    rows = realized_gains_report(journal, year)
    fields = ["symbol", "qty", "open_ts", "close_ts", "proceeds", "basis",
              "realized_pnl", "wash_disallowed", "allowed_pnl", "term"]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in rows:
            w.writerow({k: getattr(r, k) for k in fields})
    return len(rows)


@dataclass
class HarvestCandidate:
    symbol: str
    qty: float
    unrealized_loss: float

    def summary(self) -> str:
        return f"{self.symbol}: {self.qty:g} sh, unrealized loss ${self.unrealized_loss:,.2f}"


def harvest_candidates(account_state, min_loss_usd: float = 100.0) -> list[HarvestCandidate]:
    """Open positions sitting on an unrealized loss beyond a threshold — candidates
    for year-end tax-loss harvesting (subject to the wash-sale window on re-entry)."""
    out = []
    for p in account_state.positions:
        if p.unrealized_pl <= -abs(min_loss_usd):
            out.append(HarvestCandidate(symbol=p.symbol, qty=p.qty,
                                        unrealized_loss=round(p.unrealized_pl, 2)))
    out.sort(key=lambda c: c.unrealized_loss)
    return out

"""Fill & tax-lot sync (polling).

Keeps the journal truthful before any decision: pull broker orders updated since
the last watermark, record fills, open/close tax lots (HIFO), flag day trades,
and warn if journal lots drift from broker positions. Websocket streaming is
Milestone 5; polling is deterministic and unit-testable.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from ..config import Config
from ..data.journal import Journal


@dataclass
class SyncReport:
    orders_seen: int = 0
    fills_recorded: int = 0
    lots_opened: int = 0
    lots_closed: int = 0
    day_trades_flagged: int = 0
    reconciliation_warnings: list[str] = None

    def __post_init__(self):
        if self.reconciliation_warnings is None:
            self.reconciliation_warnings = []


def _parse_ts(value) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return datetime.fromisoformat(str(value))


def sync_fills(config: Config, journal: Journal, broker) -> SyncReport:
    report = SyncReport()

    watermark = journal.get_state("last_fill_sync")
    since = (
        _parse_ts(watermark)
        if watermark
        else datetime.now(timezone.utc) - timedelta(days=7)
    )

    orders = broker.list_orders_since(since)
    newest = since
    for order in orders:
        report.orders_seen += 1
        updated = _parse_ts(getattr(order, "updated_at", None) or getattr(order, "submitted_at"))
        newest = max(newest, updated)

        filled_qty = float(getattr(order, "filled_qty", 0) or 0)
        status = str(getattr(order, "status", "")).lower()
        if filled_qty <= 0 or "filled" not in status:
            continue

        broker_id = str(order.id)
        if journal.get_state(f"synced_fill:{broker_id}"):
            continue  # idempotent: never double-record a fill

        symbol = order.symbol
        side = str(getattr(order, "side", "")).lower().replace("orderside.", "")
        side = "buy" if "buy" in side else "sell"
        price = float(getattr(order, "filled_avg_price", 0) or 0)
        fees = float(getattr(order, "commission", 0) or 0)

        order_id = journal.record_order(
            proposal_id=None, mode=config.limits.mode, symbol=symbol, side=side,
            qty=filled_qty, order_type="limit", limit_price=price,
            broker_order_id=broker_id,
        )
        journal.record_fill(order_id, qty=filled_qty, price=price, fees_usd=fees)
        report.fills_recorded += 1

        if side == "buy":
            journal.open_lot(symbol=symbol, qty=filled_qty, price=price)
            report.lots_opened += 1
        else:
            closed_same_day = _close_lots_hifo(journal, symbol, filled_qty, price, updated)
            report.lots_closed += closed_same_day["closed"]
            if closed_same_day["day_trade"]:
                journal.conn.execute(
                    "UPDATE orders SET is_day_trade=1 WHERE id=?", (order_id,)
                )
                journal.conn.commit()
                report.day_trades_flagged += 1

        journal.set_state(f"synced_fill:{broker_id}", updated.isoformat())

    journal.set_state("last_fill_sync", newest.isoformat())

    _reconcile(journal, broker, report)
    detail = (f"orders={report.orders_seen} fills={report.fills_recorded} "
              f"lots+{report.lots_opened}/-{report.lots_closed} "
              f"daytrades={report.day_trades_flagged}")
    journal.heartbeat("sync_fills", status="ok", detail=detail)
    return report


def _close_lots_hifo(journal: Journal, symbol: str, qty: float, price: float,
                     close_dt: datetime) -> dict:
    """Close open lots highest-cost-first to minimize realized gain. Returns
    count closed and whether any closed lot was opened the same day (day trade)."""
    lots = journal.open_lots(symbol)
    lots.sort(key=lambda lot: lot["open_price"], reverse=True)  # HIFO
    remaining = qty
    closed = 0
    day_trade = False
    for lot in lots:
        if remaining <= 0:
            break
        opened = _parse_ts(lot["open_ts"])
        if opened.date() == close_dt.date():
            day_trade = True
        # Full-lot close (partial-lot splitting is a M3 refinement).
        journal.close_lot(lot["id"], price=price, ts=close_dt.isoformat())
        remaining -= lot["qty"]
        closed += 1
    return {"closed": closed, "day_trade": day_trade}


def _reconcile(journal: Journal, broker, report: SyncReport) -> None:
    """Warn if journal open-lot quantities disagree with broker positions."""
    try:
        state = broker.get_account_state(journal)
    except Exception as e:  # reconciliation is best-effort
        report.reconciliation_warnings.append(f"could not fetch positions: {e}")
        return
    broker_qty = {p.symbol: p.qty for p in state.positions}
    journal_qty: dict[str, float] = {}
    for lot in journal.open_lots():
        journal_qty[lot["symbol"]] = journal_qty.get(lot["symbol"], 0) + lot["qty"]

    for symbol in set(broker_qty) | set(journal_qty):
        b = broker_qty.get(symbol, 0)
        j = journal_qty.get(symbol, 0)
        if abs(b - j) > 1e-6:
            msg = f"{symbol}: broker={b:g} journal_lots={j:g}"
            report.reconciliation_warnings.append(msg)
            journal.heartbeat("reconcile", status="warn", detail=msg)

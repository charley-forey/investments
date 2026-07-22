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
from ..resilience import RetryConfig, with_retry


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


# Alpaca's `after` filters on submitted_at, not updated_at. A tight watermark
# advanced past an order's submission time permanently hides later fills — the
# day-one AAPL bug. Always query a wide rolling lookback; synced_qty:* makes
# re-processing idempotent.
_FILL_LOOKBACK_DAYS = 3


def sync_fills(config: Config, journal: Journal, broker) -> SyncReport:
    report = SyncReport()

    since = datetime.now(timezone.utc) - timedelta(days=_FILL_LOOKBACK_DAYS)

    orders = with_retry(lambda: broker.list_orders_since(since),
                        config=RetryConfig(retries=2))
    newest = since
    terminal = {"canceled", "cancelled", "rejected", "expired", "done_for_day"}
    for order in orders:
        report.orders_seen += 1
        updated = _parse_ts(getattr(order, "updated_at", None) or getattr(order, "submitted_at"))
        newest = max(newest, updated)

        broker_id = str(order.id)
        status = str(getattr(order, "status", "")).lower()
        filled_qty = float(getattr(order, "filled_qty", 0) or 0)

        # Terminal, unfilled states: record the order's final status once, no fill.
        if any(t in status for t in terminal) and filled_qty <= 0:
            if not journal.get_state(f"order_status:{broker_id}"):
                journal.set_state(f"order_status:{broker_id}", status)
            continue

        if filled_qty <= 0:
            continue  # still working / pending, nothing filled yet

        # Partial-fill safe: record only the INCREMENTAL delta since the last sync,
        # so a partially-filled order that completes later doesn't double-count and
        # its remainder isn't lost.
        already = float(journal.get_state(f"synced_qty:{broker_id}", "0") or 0)
        delta = filled_qty - already
        if delta <= 1e-9:
            continue  # nothing new since last sync

        symbol = order.symbol
        side = str(getattr(order, "side", "")).lower().replace("orderside.", "")
        side = "buy" if "buy" in side else "sell"
        price = float(getattr(order, "filled_avg_price", 0) or 0)
        fees = float(getattr(order, "commission", 0) or 0)

        # Exact strategy attribution via the deterministic client_order_id we set
        # at submission (prop-<proposal_id>); fall back to the symbol heuristic.
        coid = getattr(order, "client_order_id", None)
        proposal = _proposal_from_client_order_id(journal, coid)
        tag = proposal["strategy_tag"] if proposal else journal.recent_strategy_tag_for(symbol)
        proposal_id = int(proposal["id"]) if proposal else None

        slippage = None
        if proposal and proposal.get("limit_price"):
            from ..execution import realized_slippage_bps
            slippage = round(realized_slippage_bps(proposal["limit_price"], price, side), 2)

        # Option orders carry an OCC symbol -> 100x multiplier, tracked per contract.
        asset_class, multiplier, lot_symbol = _classify(symbol)

        order_id = journal.record_order(
            proposal_id=proposal_id, mode=config.limits.mode, symbol=symbol, side=side,
            qty=delta, order_type="limit", limit_price=price,
            broker_order_id=broker_id,
        )
        journal.record_fill(order_id, qty=delta, price=price, fees_usd=fees,
                            slippage_bps=slippage)
        report.fills_recorded += 1

        if side == "buy":
            journal.open_lot(symbol=lot_symbol, qty=delta, price=price,
                             strategy_tag=tag, multiplier=multiplier,
                             asset_class=asset_class, proposal_id=proposal_id)
            report.lots_opened += 1
        else:
            closed = _close_lots_hifo(journal, lot_symbol, delta, price, updated)
            report.lots_closed += closed["closed"]
            if closed["day_trade"]:
                journal.conn.execute(
                    "UPDATE orders SET is_day_trade=1 WHERE id=?", (order_id,)
                )
                journal.conn.commit()
                report.day_trades_flagged += 1

        # Advance the recorded-qty watermark to the cumulative filled qty.
        journal.set_state(f"synced_qty:{broker_id}", str(filled_qty))

    journal.set_state("last_fill_sync", newest.isoformat())

    _reconcile(config, journal, broker, report)
    detail = (f"orders={report.orders_seen} fills={report.fills_recorded} "
              f"lots+{report.lots_opened}/-{report.lots_closed} "
              f"daytrades={report.day_trades_flagged}")
    journal.heartbeat("sync_fills", status="ok", detail=detail)
    if report.fills_recorded:
        try:
            from .. import notify
            notify.notify_fill(config, journal, report)
        except Exception:
            pass
    return report


def _proposal_from_client_order_id(journal: Journal, client_order_id) -> dict | None:
    """Map an Alpaca order back to its originating proposal via the prop-<id>
    client_order_id we set at submission."""
    if not client_order_id or not str(client_order_id).startswith("prop-"):
        return None
    try:
        pid = int(str(client_order_id).split("-", 1)[1])
    except (ValueError, IndexError):
        return None
    return journal.get_proposal(pid)


def _classify(symbol: str) -> tuple[str, float, str]:
    """Return (asset_class, multiplier, lot_symbol). Options (OCC symbols) get a
    100x contract multiplier and are keyed by their full OCC symbol."""
    from .occ import parse_occ

    try:
        parse_occ(symbol)
        return "option", 100.0, symbol.upper()
    except ValueError:
        return "stock", 1.0, symbol.upper()


def _close_lots_hifo(journal: Journal, symbol: str, qty: float, price: float,
                     close_dt: datetime) -> dict:
    """Close open lots highest-cost-first to minimize realized gain, splitting the
    final lot if the sell quantity doesn't consume it whole. Returns the count of
    lot-closings and whether any closed lot was opened the same day (day trade)."""
    lots = journal.open_lots(symbol)
    lots.sort(key=lambda lot: lot["open_price"], reverse=True)  # HIFO
    remaining = qty
    closed = 0
    day_trade = False
    for lot in lots:
        if remaining <= 1e-9:
            break
        opened = _parse_ts(lot["open_ts"])
        if opened.date() == close_dt.date():
            day_trade = True
        take = min(remaining, lot["qty"])
        journal.close_lot_qty(lot["id"], qty=take, price=price, ts=close_dt.isoformat())
        remaining -= take
        closed += 1
    return {"closed": closed, "day_trade": day_trade}


def _is_cancellable_entry(order) -> bool:
    """Only cancel entry orders we submitted (client_order_id prop-<id>).
    Bracket stop/target legs get broker-generated IDs — never cancel those."""
    status = str(getattr(order, "status", "") or "").lower()
    if "held" in status:
        return False
    coid = str(getattr(order, "client_order_id", None) or "")
    return coid.startswith("prop-")


def cancel_stale_orders(config: Config, journal: Journal, broker) -> int:
    """Cancel working (unfilled) *entry* orders older than the configured TTL.
    Protective bracket legs are never cancelled. Returns the number cancelled."""
    ttl = config.limits.orders.stale_order_ttl_minutes
    if ttl <= 0:
        return 0
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=ttl)
    cancelled = 0
    try:
        orders = broker.list_open_orders()
    except Exception as e:
        journal.heartbeat("cancel_stale", status="error", detail=str(e))
        return 0
    for order in orders:
        if not _is_cancellable_entry(order):
            continue
        submitted = _parse_ts(getattr(order, "submitted_at", None)
                              or getattr(order, "created_at", None))
        if submitted <= cutoff:
            try:
                broker.cancel_order(str(order.id))
                cancelled += 1
            except Exception:
                continue
    if cancelled:
        journal.heartbeat("cancel_stale", detail=f"cancelled {cancelled} stale order(s)")
    return cancelled


def _reconcile(config: Config, journal: Journal, broker, report: SyncReport) -> None:
    """Compare journal open-lot quantities to broker positions. Drift beyond the
    tolerance is a warning and — if configured — trips a halt flag that blocks new
    opening trades until resolved (closes remain allowed)."""
    try:
        state = broker.get_account_state(journal)
    except Exception as e:  # reconciliation is best-effort
        report.reconciliation_warnings.append(f"could not fetch positions: {e}")
        return
    tol = config.limits.reconciliation.tolerance_shares
    broker_qty = {p.symbol: p.qty for p in state.positions}
    journal_qty: dict[str, float] = {}
    for lot in journal.open_lots():
        journal_qty[lot["symbol"]] = journal_qty.get(lot["symbol"], 0) + lot["qty"]

    mismatches = []
    for symbol in set(broker_qty) | set(journal_qty):
        b = broker_qty.get(symbol, 0)
        j = journal_qty.get(symbol, 0)
        if abs(b - j) > tol:
            msg = f"{symbol}: broker={b:g} journal_lots={j:g}"
            mismatches.append(msg)
            report.reconciliation_warnings.append(msg)

    if mismatches and config.limits.reconciliation.halt_on_mismatch:
        journal.set_state("reconcile_halt", "; ".join(mismatches))
        journal.heartbeat("reconcile", status="warn",
                          detail=f"HALT: {len(mismatches)} mismatch(es)")
    else:
        # Clean sync clears any prior halt.
        if journal.get_state("reconcile_halt"):
            journal.set_state("reconcile_halt", "")
        for msg in mismatches:
            journal.heartbeat("reconcile", status="warn", detail=msg)

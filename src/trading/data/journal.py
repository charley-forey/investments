"""SQLite journal: the audit trail for every proposal, verdict, order, fill,
tax lot, score, and system event. Everything the system decides or does lands here.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS proposals (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    agent TEXT NOT NULL,                -- which agent (or 'human') proposed it
    strategy_tag TEXT NOT NULL DEFAULT 'manual',
    symbol TEXT NOT NULL,
    asset_class TEXT NOT NULL,          -- 'stock' | 'option'
    side TEXT NOT NULL,                 -- 'buy' | 'sell'
    qty REAL NOT NULL,
    order_type TEXT NOT NULL,           -- 'limit' | 'market'
    limit_price REAL,
    stop_price REAL,                    -- planned stop for sizing (stocks)
    legs_json TEXT,                     -- options legs, JSON
    thesis TEXT,
    expected_edge_usd REAL,
    max_loss_usd REAL,
    confidence REAL,
    status TEXT NOT NULL DEFAULT 'proposed',
        -- proposed | rejected | vetoed | pending_approval | submitted | filled | canceled
    discovery_source TEXT,
    score_at_entry REAL
);

CREATE TABLE IF NOT EXISTS verdicts (
    id INTEGER PRIMARY KEY,
    proposal_id INTEGER NOT NULL REFERENCES proposals(id),
    ts TEXT NOT NULL,
    source TEXT NOT NULL,               -- 'guardrail' | 'risk_agent' | 'human'
    verdict TEXT NOT NULL,              -- 'approve' | 'reject' | 'veto'
    rule TEXT,                          -- which guardrail rule fired
    reason TEXT
);

CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY,
    proposal_id INTEGER REFERENCES proposals(id),
    broker_order_id TEXT,
    ts TEXT NOT NULL,
    mode TEXT NOT NULL,                 -- 'paper' | 'live'
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    qty REAL NOT NULL,
    order_type TEXT NOT NULL,
    limit_price REAL,
    status TEXT NOT NULL DEFAULT 'submitted',
    is_day_trade INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS fills (
    id INTEGER PRIMARY KEY,
    order_id INTEGER NOT NULL REFERENCES orders(id),
    ts TEXT NOT NULL,
    qty REAL NOT NULL,
    price REAL NOT NULL,
    fees_usd REAL NOT NULL DEFAULT 0,
    est_cost_usd REAL                   -- pre-trade estimated cost (spread+fees+slippage)
);

CREATE TABLE IF NOT EXISTS tax_lots (
    id INTEGER PRIMARY KEY,
    symbol TEXT NOT NULL,
    qty REAL NOT NULL,
    open_ts TEXT NOT NULL,
    open_price REAL NOT NULL,
    close_ts TEXT,
    close_price REAL,
    realized_pnl REAL,
    term TEXT,                          -- 'short' | 'long' (at close)
    wash_sale_flag INTEGER NOT NULL DEFAULT 0,
    strategy_tag TEXT,                  -- attributed strategy for per-tag stats
    scored INTEGER NOT NULL DEFAULT 0   -- 1 once the scorer has recorded it
);

CREATE TABLE IF NOT EXISTS scores (
    id INTEGER PRIMARY KEY,
    proposal_id INTEGER REFERENCES proposals(id),
    ts TEXT NOT NULL,
    strategy_tag TEXT,
    grade TEXT,
    thesis_adherence REAL,
    pnl_usd REAL,
    notes TEXT
);

CREATE TABLE IF NOT EXISTS kv_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS heartbeats (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    job TEXT NOT NULL,
    status TEXT NOT NULL,
    detail TEXT
);

CREATE TABLE IF NOT EXISTS equity_snapshots (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    equity REAL NOT NULL,
    cash REAL,
    buying_power REAL
);

CREATE TABLE IF NOT EXISTS reasoning (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    proposal_id INTEGER,
    agent TEXT NOT NULL,
    reasoning TEXT,
    tool_calls_json TEXT
);

CREATE TABLE IF NOT EXISTS usage (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    cycle TEXT,
    agent TEXT,
    model TEXT,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd REAL NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS signal_snapshot (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    cycle TEXT,
    symbol TEXT NOT NULL,
    bid REAL,
    ask REAL,
    last REAL,
    spread_bps REAL,
    features_json TEXT,
    sentiment REAL,
    mention_count INTEGER,
    atm_iv REAL,
    iv_rank REAL,
    pc_skew REAL
);
CREATE INDEX IF NOT EXISTS idx_snapshot_symbol ON signal_snapshot (symbol, id);

CREATE TABLE IF NOT EXISTS proposal_outcomes (
    id INTEGER PRIMARY KEY,
    proposal_id INTEGER NOT NULL UNIQUE REFERENCES proposals(id),
    ts TEXT NOT NULL,
    horizon_days INTEGER NOT NULL,
    entry_price REAL,
    stop_price REAL,
    target_price REAL,
    max_favorable_usd REAL,
    max_adverse_usd REAL,
    hypothetical_pnl REAL,
    stop_hit INTEGER NOT NULL DEFAULT 0,
    target_hit INTEGER NOT NULL DEFAULT 0,
    verdict_was_right INTEGER,
    notes TEXT
);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Journal:
    def __init__(self, db_path: str | Path):
        db_path = Path(db_path)
        from .db_util import open_connection
        # busy_timeout so the daemon + dashboard + CLI writers wait on the lock
        # instead of raising "database is locked".
        self.conn = open_connection(db_path)
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.commit()

    def _migrate(self) -> None:
        """Additive migrations for DBs created by an earlier schema version."""
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(tax_lots)")}
        if "strategy_tag" not in cols:
            self.conn.execute("ALTER TABLE tax_lots ADD COLUMN strategy_tag TEXT")
        if "scored" not in cols:
            self.conn.execute("ALTER TABLE tax_lots ADD COLUMN scored INTEGER NOT NULL DEFAULT 0")
        if "multiplier" not in cols:
            self.conn.execute("ALTER TABLE tax_lots ADD COLUMN multiplier REAL NOT NULL DEFAULT 1")
        if "asset_class" not in cols:
            self.conn.execute("ALTER TABLE tax_lots ADD COLUMN asset_class TEXT NOT NULL DEFAULT 'stock'")
        if "wash_disallowed" not in cols:
            self.conn.execute("ALTER TABLE tax_lots ADD COLUMN wash_disallowed REAL NOT NULL DEFAULT 0")
        if "wash_adjusted" not in cols:
            self.conn.execute("ALTER TABLE tax_lots ADD COLUMN wash_adjusted INTEGER NOT NULL DEFAULT 0")
        fill_cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(fills)")}
        if "slippage_bps" not in fill_cols:
            self.conn.execute("ALTER TABLE fills ADD COLUMN slippage_bps REAL")
        snap_cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(signal_snapshot)")}
        for col in ("atm_iv", "iv_rank", "pc_skew"):
            if col not in snap_cols:
                self.conn.execute(f"ALTER TABLE signal_snapshot ADD COLUMN {col} REAL")
        lot_cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(tax_lots)")}
        if "proposal_id" not in lot_cols:
            self.conn.execute("ALTER TABLE tax_lots ADD COLUMN proposal_id INTEGER")
        # Ensure proposal_outcomes exists on DBs created before this table was added.
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS proposal_outcomes ("
            "id INTEGER PRIMARY KEY, "
            "proposal_id INTEGER NOT NULL UNIQUE REFERENCES proposals(id), "
            "ts TEXT NOT NULL, horizon_days INTEGER NOT NULL, "
            "entry_price REAL, stop_price REAL, target_price REAL, "
            "max_favorable_usd REAL, max_adverse_usd REAL, "
            "hypothetical_pnl REAL, stop_hit INTEGER NOT NULL DEFAULT 0, "
            "target_hit INTEGER NOT NULL DEFAULT 0, "
            "verdict_was_right INTEGER, notes TEXT)"
        )
        prop_cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(proposals)")}
        if "discovery_source" not in prop_cols:
            self.conn.execute("ALTER TABLE proposals ADD COLUMN discovery_source TEXT")
        if "score_at_entry" not in prop_cols:
            self.conn.execute("ALTER TABLE proposals ADD COLUMN score_at_entry REAL")

    def close(self) -> None:
        self.conn.close()

    # -- proposals / verdicts -------------------------------------------------

    def record_proposal(
        self,
        *,
        agent: str,
        symbol: str,
        asset_class: str,
        side: str,
        qty: float,
        order_type: str,
        strategy_tag: str = "manual",
        limit_price: float | None = None,
        stop_price: float | None = None,
        legs: list[dict] | None = None,
        thesis: str | None = None,
        expected_edge_usd: float | None = None,
        max_loss_usd: float | None = None,
        confidence: float | None = None,
        discovery_source: str | None = None,
        score_at_entry: float | None = None,
    ) -> int:
        cur = self.conn.execute(
            """INSERT INTO proposals
               (ts, agent, strategy_tag, symbol, asset_class, side, qty, order_type,
                limit_price, stop_price, legs_json, thesis, expected_edge_usd,
                max_loss_usd, confidence, discovery_source, score_at_entry)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                utcnow(), agent, strategy_tag, symbol.upper(), asset_class, side, qty,
                order_type, limit_price, stop_price,
                json.dumps(legs) if legs else None, thesis, expected_edge_usd,
                max_loss_usd, confidence, discovery_source, score_at_entry,
            ),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def record_verdict(
        self,
        proposal_id: int,
        *,
        source: str,
        verdict: str,
        rule: str | None = None,
        reason: str | None = None,
    ) -> None:
        self.conn.execute(
            "INSERT INTO verdicts (proposal_id, ts, source, verdict, rule, reason) VALUES (?,?,?,?,?,?)",
            (proposal_id, utcnow(), source, verdict, rule, reason),
        )
        self.conn.commit()

    def set_proposal_status(self, proposal_id: int, status: str) -> None:
        self.conn.execute("UPDATE proposals SET status=? WHERE id=?", (status, proposal_id))
        self.conn.commit()

    def get_proposal(self, proposal_id: int) -> dict | None:
        row = self.conn.execute("SELECT * FROM proposals WHERE id=?", (proposal_id,)).fetchone()
        return dict(row) if row else None

    def verdicts_for(self, proposal_id: int) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM verdicts WHERE proposal_id=? ORDER BY id", (proposal_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    def pending_approvals(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM proposals WHERE status='pending_approval' ORDER BY id"
        ).fetchall()
        return [dict(r) for r in rows]

    # -- orders / fills -------------------------------------------------------

    def record_order(
        self,
        *,
        proposal_id: int | None,
        mode: str,
        symbol: str,
        side: str,
        qty: float,
        order_type: str,
        limit_price: float | None,
        broker_order_id: str | None = None,
        is_day_trade: bool = False,
    ) -> int:
        cur = self.conn.execute(
            """INSERT INTO orders
               (proposal_id, broker_order_id, ts, mode, symbol, side, qty, order_type,
                limit_price, is_day_trade)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                proposal_id, broker_order_id, utcnow(), mode, symbol.upper(), side, qty,
                order_type, limit_price, int(is_day_trade),
            ),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def record_fill(
        self, order_id: int, *, qty: float, price: float,
        fees_usd: float = 0.0, est_cost_usd: float | None = None,
        slippage_bps: float | None = None,
    ) -> int:
        cur = self.conn.execute(
            "INSERT INTO fills (order_id, ts, qty, price, fees_usd, est_cost_usd, "
            "slippage_bps) VALUES (?,?,?,?,?,?,?)",
            (order_id, utcnow(), qty, price, fees_usd, est_cost_usd, slippage_bps),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def recorded_slippage(self) -> list[float]:
        rows = self.conn.execute(
            "SELECT slippage_bps FROM fills WHERE slippage_bps IS NOT NULL"
        ).fetchall()
        return [r["slippage_bps"] for r in rows]

    def trades_since(self, since: datetime) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM orders WHERE ts >= ?",
            (since.astimezone(timezone.utc).isoformat(timespec="seconds"),),
        ).fetchone()
        return int(row["n"])

    def set_order_status(self, broker_order_id: str, status: str) -> None:
        self.conn.execute(
            "UPDATE orders SET status=? WHERE broker_order_id=?", (status, broker_order_id))
        self.conn.commit()

    def day_trades_last_n_days(self, days: int = 5) -> int:
        since = datetime.now(timezone.utc) - timedelta(days=days)
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM orders WHERE is_day_trade=1 AND ts >= ?",
            (since.isoformat(timespec="seconds"),),
        ).fetchone()
        return int(row["n"])

    # -- tax lots / wash sale -------------------------------------------------

    def open_lot(
        self, *, symbol: str, qty: float, price: float,
        ts: str | None = None, strategy_tag: str | None = None,
        multiplier: float = 1.0, asset_class: str = "stock",
        proposal_id: int | None = None,
    ) -> int:
        cur = self.conn.execute(
            "INSERT INTO tax_lots (symbol, qty, open_ts, open_price, strategy_tag, "
            "multiplier, asset_class, proposal_id) VALUES (?,?,?,?,?,?,?,?)",
            (symbol.upper(), qty, ts or utcnow(), price, strategy_tag,
             multiplier, asset_class, proposal_id),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def recent_strategy_tag_for(self, symbol: str) -> str | None:
        """Best-effort attribution: the strategy_tag of the most recent submitted
        proposal for this symbol. Used to tag lots opened via broker sync."""
        row = self.conn.execute(
            "SELECT strategy_tag FROM proposals WHERE symbol=? AND status='submitted' "
            "ORDER BY id DESC LIMIT 1",
            (symbol.upper(),),
        ).fetchone()
        return row["strategy_tag"] if row else None

    def all_closed_lots(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM tax_lots WHERE close_ts IS NOT NULL ORDER BY close_ts"
        ).fetchall()
        return [dict(r) for r in rows]

    def loss_lots_needing_wash_check(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM tax_lots WHERE close_ts IS NOT NULL AND realized_pnl < 0 "
            "AND wash_sale_flag = 0 ORDER BY close_ts"
        ).fetchall()
        return [dict(r) for r in rows]

    def lots_opened_between(self, symbol: str, start_iso: str, end_iso: str,
                            exclude_id: int | None = None) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM tax_lots WHERE symbol=? AND open_ts >= ? AND open_ts <= ? "
            "ORDER BY open_ts",
            (symbol.upper(), start_iso, end_iso),
        ).fetchall()
        return [dict(r) for r in rows if exclude_id is None or r["id"] != exclude_id]

    def apply_basis_adjustment(self, lot_id: int, *, new_open_price: float,
                               new_open_ts: str) -> None:
        self.conn.execute(
            "UPDATE tax_lots SET open_price=?, open_ts=?, wash_adjusted=1 WHERE id=?",
            (new_open_price, new_open_ts, lot_id),
        )
        self.conn.commit()

    def flag_wash_sale(self, lot_id: int, disallowed: float) -> None:
        self.conn.execute(
            "UPDATE tax_lots SET wash_sale_flag=1, wash_disallowed=? WHERE id=?",
            (disallowed, lot_id),
        )
        self.conn.commit()

    def unscored_closed_lots(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM tax_lots WHERE close_ts IS NOT NULL AND scored=0 ORDER BY close_ts"
        ).fetchall()
        return [dict(r) for r in rows]

    def mark_lot_scored(self, lot_id: int) -> None:
        self.conn.execute("UPDATE tax_lots SET scored=1 WHERE id=?", (lot_id,))
        self.conn.commit()

    def close_lot(
        self, lot_id: int, *, price: float, ts: str | None = None,
        long_term_days: int = 365,
    ) -> dict:
        row = self.conn.execute("SELECT * FROM tax_lots WHERE id=?", (lot_id,)).fetchone()
        if row is None:
            raise ValueError(f"no tax lot {lot_id}")
        close_ts = ts or utcnow()
        opened = datetime.fromisoformat(row["open_ts"])
        closed = datetime.fromisoformat(close_ts)
        holding_days = (closed - opened).days
        term = "long" if holding_days > long_term_days else "short"
        mult = row["multiplier"] if "multiplier" in row.keys() else 1.0
        realized = (price - row["open_price"]) * row["qty"] * (mult or 1.0)
        self.conn.execute(
            "UPDATE tax_lots SET close_ts=?, close_price=?, realized_pnl=?, term=? WHERE id=?",
            (close_ts, price, realized, term, lot_id),
        )
        self.conn.commit()
        return {"realized_pnl": realized, "term": term, "holding_days": holding_days}

    def close_lot_qty(
        self, lot_id: int, *, qty: float, price: float, ts: str | None = None,
        long_term_days: int = 365,
    ) -> dict:
        """Close `qty` units of a lot. If qty >= the lot's size, closes it fully;
        otherwise splits off a closed child lot and reduces the remainder — so
        partial exits produce correct per-lot basis and realized P&L."""
        row = self.conn.execute("SELECT * FROM tax_lots WHERE id=?", (lot_id,)).fetchone()
        if row is None or row["close_ts"] is not None:
            raise ValueError(f"lot {lot_id} not open")
        lot_qty = row["qty"]
        close_ts = ts or utcnow()
        opened = datetime.fromisoformat(row["open_ts"])
        holding_days = (datetime.fromisoformat(close_ts) - opened).days
        term = "long" if holding_days > long_term_days else "short"
        mult = (row["multiplier"] if "multiplier" in row.keys() else 1.0) or 1.0

        if qty >= lot_qty - 1e-9:
            realized = (price - row["open_price"]) * lot_qty * mult
            self.conn.execute(
                "UPDATE tax_lots SET close_ts=?, close_price=?, realized_pnl=?, term=? WHERE id=?",
                (close_ts, price, realized, term, lot_id),
            )
            self.conn.commit()
            return {"realized_pnl": realized, "term": term, "qty_closed": lot_qty,
                    "child_lot_id": lot_id}

        # Partial: closed child row for `qty`, parent keeps the remainder.
        realized = (price - row["open_price"]) * qty * mult
        pid = row["proposal_id"] if "proposal_id" in row.keys() else None
        cur = self.conn.execute(
            """INSERT INTO tax_lots
               (symbol, qty, open_ts, open_price, close_ts, close_price, realized_pnl,
                term, strategy_tag, multiplier, asset_class, proposal_id)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (row["symbol"], qty, row["open_ts"], row["open_price"], close_ts, price,
             realized, term, row["strategy_tag"], mult,
             row["asset_class"] if "asset_class" in row.keys() else "stock", pid),
        )
        self.conn.execute("UPDATE tax_lots SET qty=? WHERE id=?", (lot_qty - qty, lot_id))
        self.conn.commit()
        return {"realized_pnl": realized, "term": term, "qty_closed": qty,
                "child_lot_id": int(cur.lastrowid)}

    def open_lots(self, symbol: str | None = None) -> list[dict]:
        q = "SELECT * FROM tax_lots WHERE close_ts IS NULL"
        args: tuple[Any, ...] = ()
        if symbol:
            q += " AND symbol=?"
            args = (symbol.upper(),)
        return [dict(r) for r in self.conn.execute(q, args).fetchall()]

    def last_realized_loss(self, symbol: str, window_days: int) -> dict | None:
        since = (datetime.now(timezone.utc) - timedelta(days=window_days)).isoformat(
            timespec="seconds"
        )
        row = self.conn.execute(
            """SELECT * FROM tax_lots
               WHERE symbol=? AND realized_pnl < 0 AND close_ts >= ?
               ORDER BY close_ts DESC LIMIT 1""",
            (symbol.upper(), since),
        ).fetchone()
        return dict(row) if row else None

    # -- kill switch / kv state ----------------------------------------------

    def get_state(self, key: str, default: str | None = None) -> str | None:
        row = self.conn.execute("SELECT value FROM kv_state WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def set_state(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO kv_state (key, value) VALUES (?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        self.conn.commit()

    def kill_switch_active(self) -> bool:
        return self.get_state("kill_switch") == "active"

    def trip_kill_switch(self, reason: str) -> None:
        self.set_state("kill_switch", "active")
        self.set_state("kill_switch_reason", reason)
        self.set_state("kill_switch_ts", utcnow())

    def reset_kill_switch(self) -> None:
        self.set_state("kill_switch", "off")

    # -- heartbeats -----------------------------------------------------------

    def heartbeat(self, job: str, status: str = "ok", detail: str | None = None) -> None:
        self.conn.execute(
            "INSERT INTO heartbeats (ts, job, status, detail) VALUES (?,?,?,?)",
            (utcnow(), job, status, detail),
        )
        self.conn.commit()

    def recent_heartbeats(self, limit: int = 20) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM heartbeats ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    def last_successful_cycle(self) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM heartbeats WHERE job LIKE 'cycle:%' AND status='end' "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return dict(row) if row else None

    # -- usage / cost ---------------------------------------------------------

    def record_usage(
        self, *, cycle: str | None, agent: str, model: str,
        input_tokens: int, output_tokens: int, cache_read_tokens: int, cost_usd: float,
    ) -> None:
        self.conn.execute(
            """INSERT INTO usage
               (ts, cycle, agent, model, input_tokens, output_tokens,
                cache_read_tokens, cost_usd)
               VALUES (?,?,?,?,?,?,?,?)""",
            (utcnow(), cycle, agent, model, input_tokens, output_tokens,
             cache_read_tokens, cost_usd),
        )
        self.conn.commit()

    def cost_since(self, since_iso: str) -> float:
        row = self.conn.execute(
            "SELECT COALESCE(SUM(cost_usd), 0) AS c FROM usage WHERE ts >= ?", (since_iso,)
        ).fetchone()
        return float(row["c"])

    # -- equity history (for the dashboard curve) ----------------------------

    def record_equity(self, *, equity: float, cash: float | None = None,
                      buying_power: float | None = None, ts: str | None = None) -> int:
        cur = self.conn.execute(
            "INSERT INTO equity_snapshots (ts, equity, cash, buying_power) VALUES (?,?,?,?)",
            (ts or utcnow(), equity, cash, buying_power),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def equity_history(self, limit: int = 500) -> list[dict]:
        rows = self.conn.execute(
            "SELECT ts, equity, cash, buying_power FROM equity_snapshots "
            "ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in reversed(rows)]

    def last_equity_ts(self) -> str | None:
        row = self.conn.execute(
            "SELECT ts FROM equity_snapshots ORDER BY id DESC LIMIT 1").fetchone()
        return row["ts"] if row else None

    def last_heartbeat(self, job: str | None = None) -> dict | None:
        if job:
            row = self.conn.execute(
                "SELECT * FROM heartbeats WHERE job=? ORDER BY id DESC LIMIT 1", (job,)
            ).fetchone()
        else:
            row = self.conn.execute(
                "SELECT * FROM heartbeats ORDER BY id DESC LIMIT 1"
            ).fetchone()
        return dict(row) if row else None

    # -- scores ---------------------------------------------------------------

    def record_score(
        self, *, strategy_tag: str | None, pnl_usd: float, term: str,
        proposal_id: int | None = None, grade: str | None = None,
        thesis_adherence: float | None = None, notes: str | None = None,
    ) -> int:
        cur = self.conn.execute(
            """INSERT INTO scores
               (proposal_id, ts, strategy_tag, grade, thesis_adherence, pnl_usd, notes)
               VALUES (?,?,?,?,?,?,?)""",
            (proposal_id, utcnow(), strategy_tag, grade, thesis_adherence, pnl_usd,
             (notes or "") + f" [term={term}]"),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def scores_for_tag(self, strategy_tag: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM scores WHERE strategy_tag=? ORDER BY id", (strategy_tag,)
        ).fetchall()
        return [dict(r) for r in rows]

    def all_scores(self) -> list[dict]:
        return [dict(r) for r in self.conn.execute("SELECT * FROM scores ORDER BY id")]

    def distinct_strategy_tags(self) -> list[str]:
        rows = self.conn.execute(
            "SELECT DISTINCT strategy_tag FROM scores WHERE strategy_tag IS NOT NULL"
        ).fetchall()
        return [r["strategy_tag"] for r in rows]

    # -- proposal counterfactual outcomes ------------------------------------

    def record_proposal_outcome(
        self, *, proposal_id: int, horizon_days: int,
        entry_price: float | None, stop_price: float | None,
        target_price: float | None, max_favorable_usd: float | None,
        max_adverse_usd: float | None, hypothetical_pnl: float | None,
        stop_hit: bool = False, target_hit: bool = False,
        verdict_was_right: bool | None = None, notes: str | None = None,
    ) -> int:
        cur = self.conn.execute(
            """INSERT INTO proposal_outcomes
               (proposal_id, ts, horizon_days, entry_price, stop_price, target_price,
                max_favorable_usd, max_adverse_usd, hypothetical_pnl,
                stop_hit, target_hit, verdict_was_right, notes)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(proposal_id) DO UPDATE SET
                 ts=excluded.ts, horizon_days=excluded.horizon_days,
                 entry_price=excluded.entry_price, stop_price=excluded.stop_price,
                 target_price=excluded.target_price,
                 max_favorable_usd=excluded.max_favorable_usd,
                 max_adverse_usd=excluded.max_adverse_usd,
                 hypothetical_pnl=excluded.hypothetical_pnl,
                 stop_hit=excluded.stop_hit, target_hit=excluded.target_hit,
                 verdict_was_right=excluded.verdict_was_right, notes=excluded.notes""",
            (proposal_id, utcnow(), horizon_days, entry_price, stop_price, target_price,
             max_favorable_usd, max_adverse_usd, hypothetical_pnl,
             int(stop_hit), int(target_hit),
             None if verdict_was_right is None else int(verdict_was_right),
             notes),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def has_proposal_outcome(self, proposal_id: int) -> bool:
        row = self.conn.execute(
            "SELECT 1 AS n FROM proposal_outcomes WHERE proposal_id=?", (proposal_id,)
        ).fetchone()
        return row is not None

    def proposals_needing_outcome(self, *, older_than_days: int = 5,
                                  statuses: tuple[str, ...] = ("vetoed", "rejected"),
                                  ) -> list[dict]:
        """Vetoed/rejected proposals old enough to grade, not yet outcome-scored."""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=older_than_days)).isoformat()
        placeholders = ",".join("?" * len(statuses))
        rows = self.conn.execute(
            f"SELECT p.* FROM proposals p "
            f"LEFT JOIN proposal_outcomes o ON o.proposal_id = p.id "
            f"WHERE p.status IN ({placeholders}) AND p.ts <= ? AND o.id IS NULL "
            f"ORDER BY p.id",
            (*statuses, cutoff),
        ).fetchall()
        return [dict(r) for r in rows]

    def all_proposal_outcomes(self) -> list[dict]:
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM proposal_outcomes ORDER BY id")]

    def recent_proposals(self, limit: int = 100) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM proposals ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    # -- reasoning capture (transparency) ------------------------------------

    def record_reasoning(self, *, proposal_id: int | None, agent: str,
                         reasoning: str, tool_calls: list | None = None) -> int:
        cur = self.conn.execute(
            "INSERT INTO reasoning (ts, proposal_id, agent, reasoning, tool_calls_json) "
            "VALUES (?,?,?,?,?)",
            (utcnow(), proposal_id, agent, reasoning,
             json.dumps(tool_calls) if tool_calls else None),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def reasoning_for(self, proposal_id: int) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM reasoning WHERE proposal_id=? ORDER BY id", (proposal_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    def cycle_log(self, limit: int = 50) -> list[dict]:
        """Per-cycle narrative rows (proposal_id IS NULL) — the agent's own summary
        of what it examined each interval even when it proposed nothing."""
        rows = self.conn.execute(
            "SELECT * FROM reasoning WHERE proposal_id IS NULL ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    # -- signal snapshots (per-interval research dataset) --------------------

    def record_snapshot(self, *, cycle: str, symbol: str, bid, ask, last,
                        spread_bps, features: dict | None,
                        sentiment, mention_count,
                        atm_iv=None, iv_rank=None, pc_skew=None) -> int:
        cur = self.conn.execute(
            "INSERT INTO signal_snapshot "
            "(ts, cycle, symbol, bid, ask, last, spread_bps, features_json, "
            "sentiment, mention_count, atm_iv, iv_rank, pc_skew) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (utcnow(), cycle, symbol.upper(), bid, ask, last, spread_bps,
             json.dumps(features) if features else None, sentiment, mention_count,
             atm_iv, iv_rank, pc_skew),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def recent_snapshots(self, symbol: str | None = None, limit: int = 50) -> list[dict]:
        if symbol:
            rows = self.conn.execute(
                "SELECT * FROM signal_snapshot WHERE symbol=? ORDER BY id DESC LIMIT ?",
                (symbol.upper(), limit),
            ).fetchall()
        else:
            # Latest snapshot per symbol: the newest id for each symbol.
            rows = self.conn.execute(
                "SELECT s.* FROM signal_snapshot s JOIN "
                "(SELECT symbol, MAX(id) AS mid FROM signal_snapshot GROUP BY symbol) m "
                "ON s.id = m.mid ORDER BY s.symbol",
            ).fetchall()
        return [dict(r) for r in rows]

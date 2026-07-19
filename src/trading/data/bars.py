"""Bar-history persistence.

Backed by SQLite for zero-dependency reliability. The store is intentionally
behind a small interface so it can be swapped for a Parquet + DuckDB backend at
scale (M13) without touching callers — that swap is an optimization, not a
correctness change.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Bar:
    symbol: str
    date: str            # YYYY-MM-DD
    open: float
    high: float
    low: float
    close: float
    volume: float


SCHEMA = """
CREATE TABLE IF NOT EXISTS bars (
    symbol TEXT NOT NULL,
    date TEXT NOT NULL,
    open REAL, high REAL, low REAL, close REAL, volume REAL,
    PRIMARY KEY (symbol, date)
);
"""


class BarStore:
    def __init__(self, db_path: str | Path):
        db_path = Path(db_path)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(db_path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def save_bars(self, bars: list[Bar]) -> int:
        """Upsert bars; returns the count written."""
        self.conn.executemany(
            """INSERT INTO bars (symbol, date, open, high, low, close, volume)
               VALUES (?,?,?,?,?,?,?)
               ON CONFLICT(symbol, date) DO UPDATE SET
                 open=excluded.open, high=excluded.high, low=excluded.low,
                 close=excluded.close, volume=excluded.volume""",
            [(b.symbol.upper(), b.date, b.open, b.high, b.low, b.close, b.volume)
             for b in bars],
        )
        self.conn.commit()
        return len(bars)

    def load_bars(self, symbol: str, start: str | None = None,
                  end: str | None = None) -> list[Bar]:
        q = "SELECT * FROM bars WHERE symbol=?"
        args: list = [symbol.upper()]
        if start:
            q += " AND date >= ?"
            args.append(start)
        if end:
            q += " AND date <= ?"
            args.append(end)
        q += " ORDER BY date"
        rows = self.conn.execute(q, args).fetchall()
        return [Bar(symbol=r["symbol"], date=r["date"], open=r["open"], high=r["high"],
                    low=r["low"], close=r["close"], volume=r["volume"]) for r in rows]

    def latest_date(self, symbol: str) -> str | None:
        row = self.conn.execute(
            "SELECT MAX(date) AS d FROM bars WHERE symbol=?", (symbol.upper(),)
        ).fetchone()
        return row["d"] if row and row["d"] else None

    def symbols(self) -> list[str]:
        rows = self.conn.execute("SELECT DISTINCT symbol FROM bars ORDER BY symbol").fetchall()
        return [r["symbol"] for r in rows]


def bars_from_alpaca_df(symbol: str, df) -> list[Bar]:
    """Convert an alpaca-py bars DataFrame to Bar rows for the store."""
    if df is None or len(df) == 0:
        return []
    out = []
    for idx, row in df.iterrows():
        ts = idx[1] if isinstance(idx, tuple) else idx
        out.append(Bar(symbol=symbol.upper(), date=str(ts)[:10],
                       open=float(row["open"]), high=float(row["high"]),
                       low=float(row["low"]), close=float(row["close"]),
                       volume=float(row["volume"])))
    return out


def ingest_symbol(store: BarStore, broker, symbol: str, days: int = 365) -> int:
    """Fetch bars from the broker and persist them. Returns rows written."""
    df = broker.get_bars(symbol, days=days)
    bars = bars_from_alpaca_df(symbol, df)
    return store.save_bars(bars)

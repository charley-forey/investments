"""Market-intelligence persistence: news, social posts, sentiment snapshots, and
curated digests. Backed by SQLite (same zero-dependency approach as BarStore) so the
system accumulates a durable, queryable, learnable record of what the market was
saying — instead of fetching and discarding it each cycle.

Dedup is by content hash; every item is ticker-linked on ingest so downstream
analytics (signal research, backtests, the strategy agent's context) can query by
symbol and by time.
"""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def content_hash(*parts: str) -> str:
    return hashlib.sha256("|".join(p or "" for p in parts).encode("utf-8")).hexdigest()[:32]


SCHEMA = """
CREATE TABLE IF NOT EXISTS news_items (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    source TEXT,
    symbol TEXT,
    headline TEXT,
    summary TEXT,
    url TEXT,
    content_hash TEXT UNIQUE
);
CREATE TABLE IF NOT EXISTS social_posts (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    platform TEXT,
    symbol TEXT,
    author TEXT,
    text TEXT,
    score INTEGER DEFAULT 0,
    content_hash TEXT UNIQUE
);
CREATE TABLE IF NOT EXISTS sentiment_snapshots (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    symbol TEXT NOT NULL,
    polarity REAL,
    mention_count INTEGER DEFAULT 0,
    source_mix TEXT
);
CREATE TABLE IF NOT EXISTS market_intel (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    scope TEXT,
    digest_md TEXT
);
CREATE INDEX IF NOT EXISTS idx_news_symbol ON news_items(symbol, ts);
CREATE INDEX IF NOT EXISTS idx_social_symbol ON social_posts(symbol, ts);
CREATE INDEX IF NOT EXISTS idx_sent_symbol ON sentiment_snapshots(symbol, ts);
"""


@dataclass
class NewsItem:
    symbol: str
    headline: str
    summary: str = ""
    source: str = ""
    url: str = ""
    ts: str | None = None


@dataclass
class SocialPost:
    symbol: str
    text: str
    platform: str = "reddit"
    author: str = ""
    score: int = 0
    ts: str | None = None


class IntelStore:
    def __init__(self, db_path: str | Path):
        db_path = Path(db_path)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(db_path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # -- news / social (deduped) ---------------------------------------------

    def save_news(self, items: list[NewsItem]) -> int:
        n = 0
        for it in items:
            h = content_hash(it.symbol, it.headline, it.source)
            try:
                self.conn.execute(
                    "INSERT INTO news_items (ts, source, symbol, headline, summary, url, "
                    "content_hash) VALUES (?,?,?,?,?,?,?)",
                    (it.ts or utcnow(), it.source, it.symbol.upper(), it.headline,
                     it.summary, it.url, h),
                )
                n += 1
            except sqlite3.IntegrityError:
                pass  # duplicate content_hash — already stored
        self.conn.commit()
        return n

    def save_social(self, posts: list[SocialPost]) -> int:
        n = 0
        for p in posts:
            h = content_hash(p.symbol, p.text, p.platform)
            try:
                self.conn.execute(
                    "INSERT INTO social_posts (ts, platform, symbol, author, text, score, "
                    "content_hash) VALUES (?,?,?,?,?,?,?)",
                    (p.ts or utcnow(), p.platform, p.symbol.upper(), p.author, p.text,
                     p.score, h),
                )
                n += 1
            except sqlite3.IntegrityError:
                pass
        self.conn.commit()
        return n

    def record_sentiment(self, *, symbol: str, polarity: float, mention_count: int,
                         source_mix: str = "", ts: str | None = None) -> int:
        cur = self.conn.execute(
            "INSERT INTO sentiment_snapshots (ts, symbol, polarity, mention_count, "
            "source_mix) VALUES (?,?,?,?,?)",
            (ts or utcnow(), symbol.upper(), polarity, mention_count, source_mix),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def save_digest(self, digest_md: str, scope: str = "market", ts: str | None = None) -> int:
        cur = self.conn.execute(
            "INSERT INTO market_intel (ts, scope, digest_md) VALUES (?,?,?)",
            (ts or utcnow(), scope, digest_md),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    # -- queries --------------------------------------------------------------

    def recent_news(self, symbol: str | None = None, limit: int = 30) -> list[dict]:
        if symbol:
            rows = self.conn.execute(
                "SELECT * FROM news_items WHERE symbol=? ORDER BY id DESC LIMIT ?",
                (symbol.upper(), limit)).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM news_items ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def recent_social(self, symbol: str | None = None, limit: int = 30) -> list[dict]:
        if symbol:
            rows = self.conn.execute(
                "SELECT * FROM social_posts WHERE symbol=? ORDER BY id DESC LIMIT ?",
                (symbol.upper(), limit)).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM social_posts ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def sentiment_history(self, symbol: str, days: int = 30) -> list[dict]:
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        rows = self.conn.execute(
            "SELECT * FROM sentiment_snapshots WHERE symbol=? AND ts >= ? ORDER BY ts",
            (symbol.upper(), since)).fetchall()
        return [dict(r) for r in rows]

    def latest_digest(self, scope: str = "market") -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM market_intel WHERE scope=? ORDER BY id DESC LIMIT 1",
            (scope,)).fetchone()
        return dict(row) if row else None

    def active_symbols(self, limit: int = 50) -> list[str]:
        rows = self.conn.execute(
            "SELECT symbol, COUNT(*) AS n FROM news_items GROUP BY symbol "
            "ORDER BY n DESC LIMIT ?", (limit,)).fetchall()
        return [r["symbol"] for r in rows]

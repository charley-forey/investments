"""Fundamentals snapshot via Yahoo Finance quoteSummary (no API key).

Cached per symbol per calendar day in a small SQLite file so agents can call
get_fundamentals freely without hammering Yahoo.
"""

from __future__ import annotations

import json
import sqlite3
import urllib.request
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from pathlib import Path

YAHOO_URL = (
    "https://query1.finance.yahoo.com/v10/finance/quoteSummary/{symbol}"
    "?modules=summaryDetail,defaultKeyStatistics,financialData,calendarEvents"
)
USER_AGENT = "trading-bot/0.1 (fundamentals; local paper research)"


@dataclass
class Fundamentals:
    symbol: str
    market_cap: float | None = None
    trailing_pe: float | None = None
    forward_pe: float | None = None
    revenue_growth: float | None = None
    profit_margin: float | None = None
    short_pct_float: float | None = None
    beta: float | None = None
    next_earnings: str | None = None

    def summary(self) -> str:
        def _fmt(v, pct=False, money=False):
            if v is None:
                return "n/a"
            if money:
                if abs(v) >= 1e12:
                    return f"${v/1e12:.2f}T"
                if abs(v) >= 1e9:
                    return f"${v/1e9:.2f}B"
                if abs(v) >= 1e6:
                    return f"${v/1e6:.1f}M"
                return f"${v:,.0f}"
            if pct:
                return f"{v*100:.1f}%"
            return f"{v:.2f}"

        return (
            f"{self.symbol}: mkt cap {_fmt(self.market_cap, money=True)} | "
            f"P/E trail {_fmt(self.trailing_pe)} / fwd {_fmt(self.forward_pe)} | "
            f"rev growth {_fmt(self.revenue_growth, pct=True)} | "
            f"margin {_fmt(self.profit_margin, pct=True)} | "
            f"short {_fmt(self.short_pct_float, pct=True)} float | "
            f"beta {_fmt(self.beta)} | next earnings {self.next_earnings or 'n/a'}"
        )


def _raw(obj, *keys):
    cur = obj
    for k in keys:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(k)
    if cur is None:
        return None
    if isinstance(cur, dict):
        return cur.get("raw", cur.get("fmt"))
    return cur


def _parse_payload(symbol: str, payload: dict) -> Fundamentals:
    result = (payload.get("quoteSummary") or {}).get("result") or []
    if not result:
        return Fundamentals(symbol=symbol)
    r = result[0]
    sd = r.get("summaryDetail") or {}
    ks = r.get("defaultKeyStatistics") or {}
    fd = r.get("financialData") or {}
    cal = r.get("calendarEvents") or {}
    earnings = (cal.get("earnings") or {}).get("earningsDate") or []
    next_earn = None
    for d in earnings:
        if isinstance(d, dict) and d.get("fmt"):
            next_earn = str(d["fmt"])[:10]
            break
        if isinstance(d, dict) and d.get("raw"):
            next_earn = datetime.fromtimestamp(int(d["raw"]), tz=timezone.utc).date().isoformat()
            break
    return Fundamentals(
        symbol=symbol.upper(),
        market_cap=_num(_raw(sd, "marketCap")),
        trailing_pe=_num(_raw(sd, "trailingPE")),
        forward_pe=_num(_raw(sd, "forwardPE") or _raw(ks, "forwardPE")),
        revenue_growth=_num(_raw(fd, "revenueGrowth")),
        profit_margin=_num(_raw(fd, "profitMargins") or _raw(ks, "profitMargins")),
        short_pct_float=_num(_raw(ks, "shortPercentOfFloat")),
        beta=_num(_raw(sd, "beta") or _raw(ks, "beta")),
        next_earnings=next_earn,
    )


def _num(v) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _fetch_yahoo(symbol: str, timeout: float = 5.0) -> Fundamentals:
    url = YAHOO_URL.format(symbol=symbol.upper())
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    return _parse_payload(symbol, payload)


class FundamentalsCache:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS fundamentals ("
            "symbol TEXT NOT NULL, as_of TEXT NOT NULL, payload TEXT NOT NULL, "
            "PRIMARY KEY (symbol, as_of))"
        )
        self.conn.commit()

    def get(self, symbol: str, as_of: str | None = None) -> Fundamentals | None:
        day = as_of or date.today().isoformat()
        row = self.conn.execute(
            "SELECT payload FROM fundamentals WHERE symbol=? AND as_of=?",
            (symbol.upper(), day),
        ).fetchone()
        if not row:
            return None
        data = json.loads(row["payload"])
        return Fundamentals(**data)

    def put(self, feats: Fundamentals, as_of: str | None = None) -> None:
        day = as_of or date.today().isoformat()
        self.conn.execute(
            "INSERT OR REPLACE INTO fundamentals(symbol, as_of, payload) VALUES (?,?,?)",
            (feats.symbol.upper(), day, json.dumps(asdict(feats))),
        )
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()


def get_fundamentals(config, symbol: str, *, fetch_fn=_fetch_yahoo) -> Fundamentals:
    """Return cached-or-fresh fundamentals for `symbol`."""
    db_path = getattr(config.settings.paths, "fundamentals_db", None)
    if not db_path:
        db_path = str(Path(config.settings.paths.journal_db).parent / "fundamentals.db")
    cache = FundamentalsCache(db_path)
    try:
        hit = cache.get(symbol)
        if hit is not None:
            return hit
        feats = fetch_fn(symbol)
        cache.put(feats)
        return feats
    finally:
        cache.close()

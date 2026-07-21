"""Earnings / event calendar refresh.

Keeps data/calendar.json current so get_calendar stays honest for event-risk.
Primary source: Yahoo Finance quoteSummary calendarEvents (no API key). Merges
with any existing manual events and drops past dates.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger("trading.calendar")

YAHOO_URL = (
    "https://query1.finance.yahoo.com/v10/finance/quoteSummary/{symbol}"
    "?modules=calendarEvents"
)
USER_AGENT = "trading-bot/0.1 (calendar refresh; local paper research)"


@dataclass
class CalendarRefreshReport:
    symbols_ok: int = 0
    symbols_failed: int = 0
    events_written: int = 0
    path: str = ""
    errors: list[str] = field(default_factory=list)


def _fetch_yahoo_earnings(symbol: str, timeout: float = 4.0) -> list[dict]:
    """Return [{date, symbol, event}] for upcoming earnings, or []."""
    url = YAHOO_URL.format(symbol=symbol.upper())
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    result = (payload.get("quoteSummary") or {}).get("result") or []
    if not result:
        return []
    cal = (result[0].get("calendarEvents") or {})
    earnings = cal.get("earnings") or {}
    dates = earnings.get("earningsDate") or []
    out = []
    for d in dates:
        # Yahoo returns either {raw: epoch} or {fmt: "YYYY-MM-DD"}
        iso = None
        if isinstance(d, dict):
            if d.get("fmt"):
                iso = str(d["fmt"])[:10]
            elif d.get("raw"):
                iso = datetime.fromtimestamp(int(d["raw"]), tz=timezone.utc).date().isoformat()
        elif isinstance(d, (int, float)):
            iso = datetime.fromtimestamp(int(d), tz=timezone.utc).date().isoformat()
        if iso:
            out.append({"date": iso, "symbol": symbol.upper(), "event": "earnings"})
    return out


def _load_existing(path: Path) -> list[dict]:
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (ValueError, OSError):
        return []


def _merge_events(existing: list[dict], fetched: list[dict],
                  *, keep_past_days: int = 0) -> list[dict]:
    today = date.today()
    cutoff = (today - timedelta(days=keep_past_days)).isoformat()
    by_key: dict[tuple, dict] = {}
    for e in existing + fetched:
        if not isinstance(e, dict):
            continue
        d = str(e.get("date") or "")[:10]
        sym = str(e.get("symbol") or "").upper()
        ev = str(e.get("event") or "event")
        if not d or d < cutoff:
            continue
        event_type = str(e.get("event_type") or (
            "macro" if not sym and ev.upper() in {
                "FOMC", "CPI", "NFP", "PCE", "GDP", "PPI", "JOBLESS",
            } else "earnings" if "earn" in ev.lower() else "event"
        ))
        by_key[(d, sym, ev)] = {
            "date": d, "symbol": sym, "event": ev, "event_type": event_type,
        }
    return sorted(by_key.values(), key=lambda x: (x["date"], x["symbol"]))


def refresh_calendar(config, *, symbols: list[str] | None = None,
                     fetch_fn=_fetch_yahoo_earnings) -> CalendarRefreshReport:
    """Refresh calendar.json for the universe. `fetch_fn` is injectable for tests."""
    import time

    from .macro_calendar import macro_event_dicts

    path = Path(config.settings.paths.calendar_file)
    path.parent.mkdir(parents=True, exist_ok=True)
    report = CalendarRefreshReport(path=str(path))
    symbols = symbols or list(config.settings.universe.core)
    fetched: list[dict] = []
    for i, sym in enumerate(symbols):
        try:
            events = fetch_fn(sym)
            # Tag earnings from Yahoo with event_type
            for e in events:
                e.setdefault("event_type", "earnings")
            fetched.extend(events)
            report.symbols_ok += 1
        except Exception as e:  # network/parse — never abort the whole refresh
            report.symbols_failed += 1
            report.errors.append(f"{sym}: {e}")
            log.debug("calendar fetch failed for %s: %s", sym, e)
        if i + 1 < len(symbols) and fetch_fn is _fetch_yahoo_earnings:
            time.sleep(0.15)

    fetched.extend(macro_event_dicts())
    merged = _merge_events(_load_existing(path), fetched)
    path.write_text(json.dumps(merged, indent=2) + "\n", encoding="utf-8")
    report.events_written = len(merged)
    return report

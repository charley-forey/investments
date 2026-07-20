"""Pluggable earnings/economic calendar seam, mirroring data.sentiment's provider
pattern. The default JsonCalendarProvider reads a local JSON file (list of
{symbol, date, kind, title}); a real earnings/economic API drops in by subclassing
CalendarProvider and returning it from get_calendar_provider — no caller changes.

Complements data.calendar_feed (which *refreshes* calendar.json from Yahoo); this
module is the *read* seam callers use. Event-risk awareness only, never a decision.
Degrades to [] when the file is absent or unreadable rather than raising."""

from __future__ import annotations

import json
from dataclasses import dataclass, asdict
from datetime import date, timedelta
from pathlib import Path


@dataclass
class CalendarEvent:
    symbol: str
    date: str          # ISO YYYY-MM-DD
    kind: str          # 'earnings' | 'economic' | ...
    title: str

    def line(self) -> str:
        return f"{self.date} {self.symbol} [{self.kind}] {self.title}".rstrip()

    def as_dict(self) -> dict:
        return asdict(self)


class CalendarProvider:
    """Pluggable calendar backend. Subclass and return from get_calendar_provider to
    swap the JSON file for a real feed (e.g. an earnings API); callers only use
    upcoming_events. BLOCKED on the actual API key — the JSON default is the safe
    fallback for tests and no-credential runs."""

    def upcoming_events(self, symbol: str | None = None, days: int = 14) -> list[CalendarEvent]:
        raise NotImplementedError


class JsonCalendarProvider(CalendarProvider):
    """Reads events from a JSON file: a list of {symbol, date, kind, title} objects.
    `title` is optional; a legacy `event` key (written by data.calendar_feed) is
    accepted as an alias for both `title` and `kind`."""

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def _load(self) -> list[dict]:
        if not self.path.exists():
            return []
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return []
        return data if isinstance(data, list) else []

    def upcoming_events(self, symbol: str | None = None, days: int = 14) -> list[CalendarEvent]:
        today = date.today()
        horizon = (today + timedelta(days=days)).isoformat()
        today_s = today.isoformat()
        sym = (symbol or "").upper()

        out: list[CalendarEvent] = []
        for e in self._load():
            if not isinstance(e, dict):
                continue
            d = str(e.get("date", ""))[:10]
            if not (today_s <= d <= horizon):
                continue
            if sym and str(e.get("symbol", "")).upper() != sym:
                continue
            legacy = e.get("event", "")
            out.append(CalendarEvent(
                symbol=str(e.get("symbol", "")).upper(),
                date=d,
                kind=str(e.get("kind", legacy) or "event"),
                title=str(e.get("title", legacy)),
            ))
        out.sort(key=lambda ev: ev.date)
        return out


def get_calendar_provider(config) -> CalendarProvider:
    """Factory: the JSON-file provider is the safe default. A real API provider is
    selected here once configured/credentialed (read config.settings/secrets and
    return e.g. an EarningsApiProvider), with no change to callers."""
    return JsonCalendarProvider(config.settings.paths.calendar_file)

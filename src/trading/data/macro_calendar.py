"""Macro / economic event dates seeded for calendar merge.

Dates are published in advance by the Fed / BLS / BEA. Keep this list current
when a new year starts; refresh_calendar merges them into data/calendar.json
alongside earnings.
"""

from __future__ import annotations

# (date ISO, event label) — symbol left blank for market-wide macros.
MACRO_EVENTS_2026: list[tuple[str, str]] = [
    # FOMC decisions (approx announced schedule)
    ("2026-01-28", "FOMC"),
    ("2026-03-18", "FOMC"),
    ("2026-04-29", "FOMC"),
    ("2026-06-17", "FOMC"),
    ("2026-07-29", "FOMC"),
    ("2026-09-16", "FOMC"),
    ("2026-11-04", "FOMC"),
    ("2026-12-16", "FOMC"),
    # CPI (BLS, typically mid-month)
    ("2026-01-13", "CPI"),
    ("2026-02-11", "CPI"),
    ("2026-03-11", "CPI"),
    ("2026-04-10", "CPI"),
    ("2026-05-12", "CPI"),
    ("2026-06-10", "CPI"),
    ("2026-07-14", "CPI"),
    ("2026-08-12", "CPI"),
    ("2026-09-11", "CPI"),
    ("2026-10-14", "CPI"),
    ("2026-11-12", "CPI"),
    ("2026-12-10", "CPI"),
    # Nonfarm payrolls (usually first Friday)
    ("2026-01-09", "NFP"),
    ("2026-02-06", "NFP"),
    ("2026-03-06", "NFP"),
    ("2026-04-03", "NFP"),
    ("2026-05-01", "NFP"),
    ("2026-06-05", "NFP"),
    ("2026-07-02", "NFP"),
    ("2026-08-07", "NFP"),
    ("2026-09-04", "NFP"),
    ("2026-10-02", "NFP"),
    ("2026-11-06", "NFP"),
    ("2026-12-04", "NFP"),
    # PCE (BEA)
    ("2026-01-30", "PCE"),
    ("2026-02-27", "PCE"),
    ("2026-03-27", "PCE"),
    ("2026-04-30", "PCE"),
    ("2026-05-29", "PCE"),
    ("2026-06-26", "PCE"),
    ("2026-07-31", "PCE"),
    ("2026-08-28", "PCE"),
    ("2026-09-25", "PCE"),
    ("2026-10-30", "PCE"),
    ("2026-11-25", "PCE"),
    ("2026-12-23", "PCE"),
    # GDP advance estimates (BEA quarterly)
    ("2026-01-29", "GDP"),
    ("2026-04-29", "GDP"),
    ("2026-07-30", "GDP"),
    ("2026-10-29", "GDP"),
]


def macro_event_dicts(*, year: int | None = None) -> list[dict]:
    """Return calendar event dicts for macro prints. Currently seeds 2026;
    filter by year if provided."""
    out = []
    for d, ev in MACRO_EVENTS_2026:
        if year is not None and not d.startswith(str(year)):
            continue
        out.append({
            "date": d,
            "symbol": "",
            "event": ev,
            "event_type": "macro",
        })
    return out

"""Regression for the 2026-07-29 blind afternoon.

A flat 25%-per-hour slice let four hours consume a 6.5-hour session: spend ran
$3.89/$3.89/$3.75/$3.16 and the budget was gone by 13:00 ET. Every intraday cycle
from 15:35 to the close logged "skipped: cost cap reached", so the system had no
decision capacity through the FOMC decision, and the postclose learning cycle ran
at cost=$0.000.

These pin the two properties that failure violated:
  1. budget survives to the end of the session
  2. the postclose reserve cannot be spent by intraday
"""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from trading.orchestrator import (
    _MIN_HOURLY_BUDGET_SHARE, _POSTCLOSE_RESERVE_USD, Orchestrator,
)

ET = ZoneInfo("America/New_York")


class _Clock:
    """Orchestrator with a frozen market-time clock and a fake spend ledger."""

    def __init__(self, orch, spend_by_hour=None):
        self.orch = orch
        self.now = datetime(2026, 7, 29, 9, 30, tzinfo=ET)
        self.spend = spend_by_hour if spend_by_hour is not None else {}
        orch._session_now = lambda: self.now
        orch.journal.cost_since = self._cost_since
        orch.journal.heartbeat = lambda *a, **k: None

    def _cost_since(self, since_iso: str) -> float:
        since = datetime.fromisoformat(since_iso).astimezone(ET)
        return sum(v for h, v in self.spend.items() if h >= since.hour)


@pytest.fixture
def orch(config, journal):
    config.settings.agents.max_daily_cost_usd = 15.0
    return Orchestrator(config, journal, broker=None, client=None)


def test_budget_survives_to_the_close(orch):
    """Walk the session spending exactly what pacing allows each hour. The day
    must not be exhausted before 16:00 -- that is the whole failure."""
    clock = _Clock(orch)
    spent_total = 0.0
    for hour in range(9, 16):
        clock.now = datetime(2026, 7, 29, hour, 0, tzinfo=ET)
        clock.spend = {hour: 0.0, **{h: 0.0 for h in range(9, hour)}}
        # replay accumulated spend as one bucket before this hour
        clock.spend = {8: spent_total, hour: 0.0}
        assert not orch._cost_capped(), f"capped at {hour}:00 with ${spent_total:.2f} spent"
        # spend right up to this hour's allowance
        allowance = 0.0
        while not orch._cost_capped():
            allowance += 0.05
            clock.spend = {8: spent_total, hour: allowance}
        spent_total += allowance
    assert spent_total <= 15.0 - _POSTCLOSE_RESERVE_USD + 0.05


def test_fomc_hour_still_has_budget_after_a_hot_morning(orch):
    """Yesterday's exact morning: $11.53 gone by 13:00. 14:00 ET (the FOMC hour)
    must still be able to think."""
    clock = _Clock(orch)
    clock.now = datetime(2026, 7, 29, 14, 0, tzinfo=ET)
    clock.spend = {9: 3.89, 10: 3.89, 11: 3.75, 13: 0.0, 14: 0.0}
    assert not orch._cost_capped(), "no capacity left for the FOMC decision"


def test_intraday_cannot_spend_the_postclose_reserve(orch):
    clock = _Clock(orch)
    clock.now = datetime(2026, 7, 29, 15, 0, tzinfo=ET)
    clock.spend = {8: 15.0 - _POSTCLOSE_RESERVE_USD, 15: 0.0}
    assert orch._cost_capped(), "intraday spent into the learning cycle's reserve"


def test_postclose_may_spend_the_reserve(orch):
    """Same ledger, the postclose caller: it must get through."""
    clock = _Clock(orch)
    clock.now = datetime(2026, 7, 29, 16, 30, tzinfo=ET)
    clock.spend = {8: 15.0 - _POSTCLOSE_RESERVE_USD, 16: 0.0}
    assert not orch._cost_capped(reserve=0.0, pace=False), \
        "learning cycle starved -- this is how lessons.md went stale"


def test_postclose_still_stops_at_the_hard_cap(orch):
    clock = _Clock(orch)
    clock.now = datetime(2026, 7, 29, 16, 30, tzinfo=ET)
    clock.spend = {8: 15.01, 16: 0.0}
    assert orch._cost_capped(reserve=0.0, pace=False)


def test_day_boundary_is_market_time_not_utc(orch):
    """UTC midnight is 20:00 ET the previous evening, so an evening's spend used
    to count against the next morning's budget."""
    clock = _Clock(orch)
    clock.now = datetime(2026, 7, 29, 9, 30, tzinfo=ET)
    # $14 spent at 21:00 ET on the 28th -- before this ET day started.
    clock.spend = {}
    orch.journal.cost_since = lambda since_iso: (
        0.0 if datetime.fromisoformat(since_iso).astimezone(ET).date() == datetime(2026, 7, 29).date()
        else 14.0
    )
    assert not orch._cost_capped(), "last night's spend suppressed this morning"

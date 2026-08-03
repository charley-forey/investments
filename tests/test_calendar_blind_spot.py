"""Regression for the earnings blackout that ran blind 2026-07-21 .. 08-03.

Yahoo started requiring a cookie+crumb on quoteSummary. Every one of the 31
universe symbols 401'd, `refresh_calendar` caught each into report.errors at
log level DEBUG, and the scheduler reported:

    calendar ok  events=28 ok=0 fail=31

`status='ok'`, because `events_written` counts the MERGED file and macro dates
are generated locally -- so the number stayed healthy while every earnings date
was missing. The agent sized entries for two weeks with no idea who was
reporting. The data-source fix matters less than this: a refresh that fetched
nothing from its only real source must not report success.
"""

import pytest

from trading.data.calendar_feed import refresh_calendar


def _boom(symbol):
    raise RuntimeError(f"HTTP Error 401: Unauthorized ({symbol})")


def _ok(symbol):
    return [{"date": "2026-09-01", "symbol": symbol, "event": "earnings"}]


def test_total_source_failure_is_visible_in_the_report(config):
    """ok=0 with a non-empty universe is the signature. Macro events still land."""
    report = refresh_calendar(config, symbols=["AAPL", "MSFT"], fetch_fn=_boom)
    assert report.symbols_ok == 0
    assert report.symbols_failed == 2
    assert len(report.errors) == 2
    # The trap: events were still written, from the local macro generator.
    assert report.events_written > 0, "macro dates mask the failure in the count"


def test_a_working_source_reports_ok(config):
    report = refresh_calendar(config, symbols=["AAPL", "MSFT"], fetch_fn=_ok)
    assert report.symbols_ok == 2
    assert report.symbols_failed == 0


def test_partial_failure_still_counts_both_sides(config):
    def flaky(symbol):
        if symbol == "MSFT":
            raise RuntimeError("timeout")
        return _ok(symbol)

    report = refresh_calendar(config, symbols=["AAPL", "MSFT"], fetch_fn=flaky)
    assert (report.symbols_ok, report.symbols_failed) == (1, 1)


@pytest.mark.parametrize("ok,failed,expect_warn", [
    (0, 31, True),    # the 07-21..08-03 outage
    (0, 0, False),    # empty universe is not a failure
    (30, 1, False),   # one flaky symbol is not an outage
])
def test_warn_condition(ok, failed, expect_warn):
    """The predicate scheduler.run_calendar_safe uses to downgrade to 'warn'."""
    assert (ok == 0 and failed > 0) is expect_warn

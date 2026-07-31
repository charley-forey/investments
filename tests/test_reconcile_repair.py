"""`trading reconcile [--repair]` — the recovery procedure that did not exist.

On the night of 2026-07-30 a multi-leg sync bug left the journal holding two
option contracts the broker did not, realized P&L reading $0 on a -$395 day, and
every downstream statistic — expectancy, per-strategy stats, lifecycle demotion,
auto-calibration — computed from the wrong book. It was repaired by hand-written
SQL against the live database, twice, at 4am. That is not a recovery procedure.

The broker is the authority: it holds the actual positions and the actual cash.
When the two disagree the journal is wrong by definition.
"""

import pytest

from trading.broker.models import PositionView
from trading.broker.sync import find_drift, repair_drift
from trading.data.journal import Journal

from conftest import make_account
from stubs import StubBroker


def _broker(positions=None):
    return StubBroker(make_account(positions=positions or []))


def _pos(symbol, qty, price=100.0, asset_class="stock"):
    p = PositionView(symbol=symbol, qty=qty, avg_entry_price=price,
                     market_value=qty * price, unrealized_pl=0.0)
    p.asset_class = asset_class
    return p


def _repair(config, journal, broker, drifts, mark=100.0):
    return repair_drift(config, journal, broker, drifts, mark_for=lambda s: mark)


# -- detection ----------------------------------------------------------------

def test_a_matching_book_reports_no_drift(config, journal):
    journal.open_lot(symbol="AAPL", qty=10, price=100.0)
    assert find_drift(config, journal, _broker([_pos("AAPL", 10)])) == []


def test_a_phantom_option_contract_is_drift(config, journal):
    """The exact 2026-07-30 state. One contract is 100 shares of exposure, so the
    share tolerance must not apply to it."""
    journal.open_lot(symbol="MSFT260814C00470000", qty=1, price=5.45,
                     multiplier=100.0, asset_class="option")
    drifts = find_drift(config, journal, _broker([]))
    assert len(drifts) == 1
    assert drifts[0].symbol == "MSFT260814C00470000"
    assert drifts[0].delta == -1


def test_a_position_the_journal_never_saw_is_drift(config, journal):
    drifts = find_drift(config, journal, _broker([_pos("NVDA", 25)]))
    assert len(drifts) == 1 and drifts[0].delta == 25


def test_fractional_share_drift_is_still_tolerated(config, journal):
    journal.open_lot(symbol="AAPL", qty=10.5, price=100.0)
    assert find_drift(config, journal, _broker([_pos("AAPL", 10)])) == []


# -- repair -------------------------------------------------------------------

def test_repair_closes_a_lot_the_broker_does_not_hold(config, journal):
    journal.open_lot(symbol="MSFT260814C00470000", qty=1, price=5.45,
                     multiplier=100.0, asset_class="option")
    broker = _broker([])
    _repair(config, journal, broker, find_drift(config, journal, broker), mark=4.20)

    assert journal.open_lots() == []
    assert find_drift(config, journal, broker) == []
    lot = journal.conn.execute(
        "SELECT realized_pnl FROM tax_lots WHERE close_ts IS NOT NULL").fetchone()
    assert lot["realized_pnl"] == pytest.approx((4.20 - 5.45) * 1 * 100)


def test_repair_opens_a_position_the_journal_is_missing(config, journal):
    broker = _broker([_pos("NVDA", 25)])
    _repair(config, journal, broker, find_drift(config, journal, broker), mark=180.0)

    lots = journal.open_lots("NVDA")
    assert len(lots) == 1 and lots[0]["qty"] == 25
    assert find_drift(config, journal, broker) == []


def test_adjustments_are_tagged_so_they_cannot_pose_as_a_strategy(config, journal):
    """Every learning surface reads tax_lots by strategy_tag. An adjustment
    attributed to a real strategy would poison its record."""
    broker = _broker([_pos("NVDA", 10)])
    _repair(config, journal, broker, find_drift(config, journal, broker))
    assert journal.open_lots("NVDA")[0]["strategy_tag"] == "reconcile-adjustment"


def test_repair_covers_a_short_the_broker_does_not_hold(config, journal):
    """Shorts carry negative qty; covering below the sale price is a gain."""
    journal.open_lot(symbol="SPY", qty=-10, price=100.0)
    broker = _broker([])
    _repair(config, journal, broker, find_drift(config, journal, broker), mark=90.0)

    assert journal.open_lots() == []
    lot = journal.conn.execute(
        "SELECT realized_pnl FROM tax_lots WHERE close_ts IS NOT NULL").fetchone()
    assert lot["realized_pnl"] == pytest.approx(100.0)


def test_repair_clears_the_halt(config, journal):
    # qty 5, not 1: a 1-share stock drift sits exactly at tolerance_shares and is
    # correctly not drift at all.
    journal.set_state("reconcile_halt", "MSFT: broker=0 journal_lots=5")
    journal.open_lot(symbol="MSFT", qty=5, price=5.0)
    broker = _broker([])
    _repair(config, journal, broker, find_drift(config, journal, broker))
    assert not journal.get_state("reconcile_halt")


def test_an_unpriceable_symbol_is_skipped_not_guessed(config, journal):
    """Better to leave one drift visible than to write a fabricated basis into
    the table every downstream statistic reads."""
    journal.open_lot(symbol="DELISTED", qty=5, price=10.0)
    broker = _broker([])
    actions = repair_drift(config, journal, broker,
                           find_drift(config, journal, broker),
                           mark_for=lambda s: None)
    assert any("SKIPPED" in a for a in actions)
    assert journal.open_lots("DELISTED"), "the lot must survive, not vanish"


def test_repair_is_idempotent(config, journal):
    broker = _broker([_pos("NVDA", 10)])
    _repair(config, journal, broker, find_drift(config, journal, broker))
    second = find_drift(config, journal, broker)
    assert second == []
    assert _repair(config, journal, broker, second) == []

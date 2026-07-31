"""Two failures from 2026-07-30 that let a bad day pass unnoticed.

1. Reconciliation did not halt on a journal-vs-broker mismatch. The journal held
   one option contract the broker did not, `abs(0 - 1) > 1` is false, and
   `tolerance_shares` is 1. That tolerance exists for fractional-SHARE drift; one
   contract is 100 shares of exposure, so tolerating it tolerates a whole position.

2. A breached stop on an option produced 39 exit proposals in 35 minutes -- 34
   left dangling -- while the position stayed open the entire time. Exit rules
   re-evaluate every cycle and a breached stop stays breached until the position
   is gone, so nothing stopped the loop.
"""

from datetime import datetime, timedelta, timezone

import pytest

from trading.broker.models import PositionView
from trading.broker.sync import _reconcile, SyncReport
from trading.data.journal import Journal

from conftest import make_account
from stubs import StubBroker


# -- reconciliation -----------------------------------------------------------

def _reconcile_with(config, journal, broker_positions):
    broker = StubBroker(make_account(positions=broker_positions))
    report = SyncReport()
    _reconcile(config, journal, broker, report)
    return report


def test_a_single_phantom_contract_halts(config, journal):
    """The exact 2026-07-30 state: journal holds 1 contract, broker holds none."""
    journal.open_lot(symbol="MSFT260814C00470000", qty=1, price=5.45,
                     multiplier=100.0, asset_class="option")
    report = _reconcile_with(config, journal, [])
    assert report.reconciliation_warnings, "a whole option position went unnoticed"
    assert journal.get_state("reconcile_halt")


def test_fractional_share_drift_is_still_tolerated(config, journal):
    """The tolerance still does its real job -- this must not become noisy."""
    journal.open_lot(symbol="AAPL", qty=10.5, price=100.0)
    report = _reconcile_with(config, journal, [
        PositionView(symbol="AAPL", qty=10.0, avg_entry_price=100.0,
                     market_value=1000.0, unrealized_pl=0.0)])
    assert not report.reconciliation_warnings


def test_a_matching_book_clears_a_prior_halt(config, journal):
    journal.set_state("reconcile_halt", "stale")
    journal.open_lot(symbol="AAPL", qty=10, price=100.0)
    _reconcile_with(config, journal, [
        PositionView(symbol="AAPL", qty=10.0, avg_entry_price=100.0,
                     market_value=1000.0, unrealized_pl=0.0)])
    assert not journal.get_state("reconcile_halt")


# -- exit dedup ---------------------------------------------------------------

class _OrderBroker(StubBroker):
    def __init__(self, *a, open_orders=None, **kw):
        super().__init__(*a, **kw)
        self._open_orders = open_orders or []


class _FakeOrder:
    def __init__(self, symbol, side):
        self.symbol, self.side, self.id = symbol, side, "x"


def _orch(config, journal, broker):
    from trading.orchestrator import Orchestrator
    return Orchestrator(config, journal, broker, client=None)


def test_a_resting_exit_blocks_a_duplicate(config, journal):
    """Re-sending stacks duplicate orders against one position."""
    broker = _OrderBroker(make_account(),
                          open_orders=[_FakeOrder("MSFT260814C00470000", "sell")])
    orch = _orch(config, journal, broker)
    assert orch._exit_already_in_flight("MSFT260814C00470000", "sell")


def test_a_resting_order_on_the_other_side_does_not_block(config, journal):
    broker = _OrderBroker(make_account(),
                          open_orders=[_FakeOrder("MSFT260814C00470000", "buy")])
    orch = _orch(config, journal, broker)
    assert not orch._exit_already_in_flight("MSFT260814C00470000", "sell")


def test_a_failed_submission_backs_off_then_retries(config, journal):
    """No resting order means submission failed. Retrying instantly just fails
    again -- but a stop that gives up is worse than one that never fired, so the
    backoff must expire."""
    from trading.orchestrator import _EXIT_RETRY_MINUTES

    orch = _orch(config, journal, _OrderBroker(make_account(), open_orders=[]))
    assert not orch._exit_already_in_flight("MSFT", "sell")   # first attempt runs
    assert orch._exit_already_in_flight("MSFT", "sell")       # immediate retry blocked

    past = datetime.now(timezone.utc) - timedelta(minutes=_EXIT_RETRY_MINUTES + 1)
    journal.set_state("exit_attempt:MSFT:sell", past.isoformat())
    assert not orch._exit_already_in_flight("MSFT", "sell"), \
        "the stop must be chased again once the backoff expires"


def test_thirty_five_minutes_of_a_breached_stop_is_not_thirty_nine_proposals(
        config, journal):
    """Replays the shape of the real failure at one cycle per minute."""
    from trading.orchestrator import _EXIT_RETRY_MINUTES

    orch = _orch(config, journal, _OrderBroker(make_account(), open_orders=[]))
    start = datetime.now(timezone.utc)
    attempts = 0
    for minute in range(35):
        prior = journal.get_state("exit_attempt:MSFT:sell")
        if prior:
            age = (start + timedelta(minutes=minute)
                   - datetime.fromisoformat(prior)).total_seconds() / 60.0
            if age < _EXIT_RETRY_MINUTES:
                continue
        journal.set_state("exit_attempt:MSFT:sell",
                          (start + timedelta(minutes=minute)).isoformat())
        attempts += 1
    assert attempts <= 8, f"{attempts} attempts in 35 minutes (was 39)"
    assert attempts >= 5, "but it must keep chasing a breached stop"


def test_an_unreadable_order_book_never_blocks_an_exit(config, journal):
    """Failing safe means EXITING. If the book cannot be read we must still try."""
    class _Broken(StubBroker):
        def list_open_orders(self):
            raise RuntimeError("api down")

    orch = _orch(config, journal, _Broken(make_account()))
    assert not orch._exit_already_in_flight("MSFT", "sell")

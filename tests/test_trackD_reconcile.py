"""Tests for pure broker/journal reconciliation."""

from trading.analytics.reconcile import reconcile
from trading.broker.models import PositionView


def _pos(symbol, qty):
    return PositionView(symbol=symbol, qty=qty, avg_entry_price=100.0,
                        market_value=qty * 100.0, unrealized_pl=0.0)


def test_clean_when_matching():
    broker = [_pos("AAPL", 10), _pos("SPY", 5)]
    journal = {"AAPL": 10, "SPY": 5}
    rep = reconcile(broker, journal)
    assert rep.is_clean
    assert "clean" in rep.summary()


def test_qty_mismatch():
    rep = reconcile([_pos("AAPL", 10)], {"AAPL": 7})
    assert not rep.is_clean
    d = rep.discrepancies[0]
    assert d.symbol == "AAPL" and d.kind == "qty_mismatch"
    assert d.broker_qty == 10 and d.journal_qty == 7 and d.delta == 3


def test_missing_in_journal_and_broker():
    # broker has TSLA journal doesn't; journal has NVDA broker doesn't
    rep = reconcile([_pos("TSLA", 4)], {"NVDA": 2})
    kinds = {d.symbol: d.kind for d in rep.discrepancies}
    assert kinds == {"TSLA": "missing_in_journal", "NVDA": "missing_in_broker"}


def test_tolerance_ignores_tiny_drift():
    rep = reconcile([_pos("AAPL", 10.0000001)], {"AAPL": 10.0}, tolerance=1e-3)
    assert rep.is_clean


def test_journal_lots_are_summed():
    # journal view given as a list of open-lot dicts for the same symbol
    lots = [{"symbol": "AAPL", "qty": 6}, {"symbol": "AAPL", "qty": 4}]
    rep = reconcile([_pos("AAPL", 10)], lots)
    assert rep.is_clean


def test_bad_input_never_raises():
    assert reconcile(None, None).is_clean
    assert reconcile("garbage", {"AAPL": None}).is_clean

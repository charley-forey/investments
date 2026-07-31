"""One multi-leg order must not be able to abort the entire fill sync.

This was the root cause of the 2026-07-30 ledger failure, and it hid behind two
other bugs. An MLEG parent order carries `symbol=None` -- the fills live on its
legs. `_classify(None)` called `parse_occ(None)`, which raised AttributeError on
`.strip()` rather than the ValueError `_classify` guards for, and that propagated
out of the whole `sync_fills` loop.

So from the first vertical onward: no fills recorded, no lots closed, no realized
P&L, and no reconciliation -- on EVERY cycle, not just the one with the option in
it. The system lost $395 and its ledger read $0.

The failure was invisible because the exception was caught into
`CycleReport.notes` and nothing rendered notes. It surfaced the moment notes were
printed, in the very first postclose summary after that change.
"""

from datetime import datetime, timedelta, timezone

import pytest

from trading.broker.occ import parse_occ
from trading.broker.sync import _expand_legs


class _Order:
    def __init__(self, symbol=None, side="buy", legs=None, filled_at=None,
                 client_order_id=None):
        self.symbol, self.side, self.legs = symbol, side, legs
        self.filled_at = filled_at
        self.updated_at = filled_at
        self.submitted_at = filled_at
        self.client_order_id = client_order_id


T0 = datetime(2026, 7, 30, 14, 0, tzinfo=timezone.utc)


# -- the crash ----------------------------------------------------------------

def test_parse_occ_rejects_none_as_a_value_error():
    """The guard callers actually write is `except ValueError`."""
    with pytest.raises(ValueError):
        parse_occ(None)


def test_parse_occ_rejects_blank():
    with pytest.raises(ValueError):
        parse_occ("   ")


def test_a_real_occ_symbol_still_parses():
    parts = parse_occ("MSFT260814C00470000")
    assert parts.underlying == "MSFT"
    assert parts.right == "call"
    assert parts.strike == pytest.approx(470.0)


# -- leg expansion ------------------------------------------------------------

def test_a_multileg_parent_is_replaced_by_its_legs():
    parent = _Order(symbol=None, filled_at=T0, client_order_id="prop-42", legs=[
        _Order(symbol="MSFT260814C00470000", side="buy", filled_at=T0),
        _Order(symbol="MSFT260814C00447500", side="sell", filled_at=T0),
    ])
    out = list(_expand_legs([parent]))
    assert [o.symbol for o in out] == ["MSFT260814C00470000", "MSFT260814C00447500"]


def test_legs_inherit_the_parents_client_order_id():
    """Attribution back to the proposal lives on the parent. Without inheriting
    it every option fill loses its strategy tag."""
    parent = _Order(symbol=None, filled_at=T0, client_order_id="prop-42", legs=[
        _Order(symbol="MSFT260814C00470000", side="buy", filled_at=T0),
    ])
    leg = list(_expand_legs([parent]))[0]
    assert leg.client_order_id == "prop-42"


def test_a_single_leg_order_passes_through_untouched():
    order = _Order(symbol="AAPL", side="buy", filled_at=T0)
    assert list(_expand_legs([order])) == [order]


def test_fills_are_returned_in_chronological_order():
    """Lot pairing is order-dependent: a close processed before its open has
    nothing to close, so it opens opposing exposure and the position doubles."""
    late = _Order(symbol="AAPL", side="sell", filled_at=T0 + timedelta(hours=2))
    early = _Order(symbol="AAPL", side="buy", filled_at=T0)
    assert list(_expand_legs([late, early])) == [early, late]


def test_an_order_with_no_timestamp_does_not_abort_the_sort():
    undated = _Order(symbol="AAPL", side="buy", filled_at=None)
    dated = _Order(symbol="AAPL", side="sell", filled_at=T0)
    out = list(_expand_legs([dated, undated]))
    assert out[0] is undated, "undated sorts first rather than raising"

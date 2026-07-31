"""Both sides of a trade reach the ledger.

On 2026-07-30 the system opened two option verticals -- its first options ever.
`sync_fills` routed every sell to `_close_lots_hifo`, so a sell-to-OPEN (the short
leg of a credit spread, and every stock short) closed nothing and was recorded
NOWHERE. The consequences compounded:

  * the journal showed two naked long legs it did not own
  * realized P&L for the day read $0
  * equity fell $395 and nothing in the ledger knew
  * reconciliation did not halt, because the drift was exactly 1 contract and
    `tolerance_shares` is 1 -- a tolerance meant for fractional SHARES, applied to
    a contract worth 100 of them

Every learning surface in this system reads tax_lots. A ledger that cannot see a
loss cannot learn from it.
"""

from datetime import datetime, timezone

import pytest

from trading.broker.sync import _apply_fill
from trading.data.journal import Journal

NOW = datetime(2026, 7, 30, 14, 0, tzinfo=timezone.utc)


def _fill(journal, symbol, side, qty, price, *, asset_class="stock", mult=1.0,
          when=NOW):
    return _apply_fill(journal, symbol, side, qty, price, when,
                       strategy_tag="t", multiplier=mult,
                       asset_class=asset_class, proposal_id=None)


# -- the defect: a sell with nothing to close ---------------------------------

def test_sell_to_open_creates_a_short_lot(journal):
    out = _fill(journal, "MSFT260814C00447500", "sell", 1, 11.55,
                asset_class="option", mult=100.0)
    assert out["opened"] == 1
    lots = journal.open_lots("MSFT260814C00447500")
    assert len(lots) == 1
    assert lots[0]["qty"] == -1, "the short leg must exist, and be short"


def test_buy_to_close_a_short_realizes_the_right_sign(journal):
    """Covering below the sale price is a GAIN. Sold at 11.55, covered at 6.55."""
    _fill(journal, "MSFT260814C00447500", "sell", 1, 11.55,
          asset_class="option", mult=100.0)
    out = _fill(journal, "MSFT260814C00447500", "buy", 1, 6.55,
                asset_class="option", mult=100.0)
    assert out["closed"] == 1
    assert journal.open_lots("MSFT260814C00447500") == []
    lot = journal.conn.execute(
        "SELECT realized_pnl FROM tax_lots WHERE symbol=?",
        ("MSFT260814C00447500",)).fetchone()
    assert lot["realized_pnl"] == pytest.approx(500.0)  # (6.55-11.55)*-1*100


def test_covering_a_short_higher_is_a_loss(journal):
    _fill(journal, "SPY", "sell", 10, 100.0)
    _fill(journal, "SPY", "buy", 10, 110.0)
    lot = journal.conn.execute(
        "SELECT realized_pnl FROM tax_lots WHERE symbol='SPY'").fetchone()
    assert lot["realized_pnl"] == pytest.approx(-100.0)


# -- the full vertical, end to end --------------------------------------------

def test_a_credit_vertical_records_both_legs_and_its_pnl(journal):
    """The exact 2026-07-30 MSFT structure: long the 470 call, short the 447.5."""
    _fill(journal, "MSFT260814C00470000", "buy", 1, 5.45, asset_class="option", mult=100.0)
    _fill(journal, "MSFT260814C00447500", "sell", 1, 11.55, asset_class="option", mult=100.0)
    assert len(journal.open_lots()) == 2, "both legs must be on the books"

    # Close it out at a loss on the spread.
    _fill(journal, "MSFT260814C00470000", "sell", 1, 3.00, asset_class="option", mult=100.0)
    _fill(journal, "MSFT260814C00447500", "buy", 1, 12.00, asset_class="option", mult=100.0)

    assert journal.open_lots() == [], "nothing may be left open"
    total = journal.conn.execute(
        "SELECT COALESCE(SUM(realized_pnl),0) t FROM tax_lots WHERE close_ts IS NOT NULL"
    ).fetchone()["t"]
    # long: (3.00-5.45)*1*100 = -245 ; short: (12.00-11.55)*-1*100 = -45
    assert total == pytest.approx(-290.0)
    assert total != 0, "a losing day must not read as $0 -- that was the bug"


# -- direction-agnostic behaviour ---------------------------------------------

def test_a_buy_still_opens_a_long_when_flat(journal):
    out = _fill(journal, "AAPL", "buy", 10, 100.0)
    assert out["opened"] == 1
    assert journal.open_lots("AAPL")[0]["qty"] == 10


def test_a_sell_still_closes_a_long_when_one_exists(journal):
    _fill(journal, "AAPL", "buy", 10, 100.0)
    out = _fill(journal, "AAPL", "sell", 10, 110.0)
    assert out["closed"] == 1 and out["opened"] == 0
    assert journal.open_lots("AAPL") == []


def test_a_sell_larger_than_the_long_flips_to_short(journal):
    """Close 10, then open 5 short -- the remainder must not silently vanish."""
    _fill(journal, "AAPL", "buy", 10, 100.0)
    out = _fill(journal, "AAPL", "sell", 15, 110.0)
    assert out["closed"] == 1 and out["opened"] == 1
    lots = journal.open_lots("AAPL")
    assert len(lots) == 1 and lots[0]["qty"] == -5


def test_partial_cover_leaves_the_rest_short(journal):
    _fill(journal, "AAPL", "sell", 10, 100.0)
    _fill(journal, "AAPL", "buy", 4, 90.0)
    lots = journal.open_lots("AAPL")
    assert len(lots) == 1
    assert lots[0]["qty"] == pytest.approx(-6), "a partly-covered short stays short"

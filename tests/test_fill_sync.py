from datetime import datetime, timedelta, timezone

from conftest import make_config

from stubs import StubBroker, StubOrder, make_account

from trading.broker.models import PositionView
from trading.broker.sync import sync_fills
from trading.data.journal import Journal


def test_buy_opens_lot_and_records_fill(tmp_path):
    config = make_config()
    journal = Journal(tmp_path / "j.db")
    order = StubOrder(id="o1", symbol="AAPL", side="buy", filled_qty=10, filled_avg_price=180.0)
    broker = StubBroker(make_account(positions=[
        PositionView(symbol="AAPL", qty=10, avg_entry_price=180.0,
                     market_value=1800.0, unrealized_pl=0.0)
    ]), orders=[order])

    report = sync_fills(config, journal, broker)
    assert report.fills_recorded == 1
    assert report.lots_opened == 1
    assert len(journal.open_lots("AAPL")) == 1


def test_fill_sync_is_idempotent(tmp_path):
    config = make_config()
    journal = Journal(tmp_path / "j.db")
    order = StubOrder(id="o1", symbol="AAPL", side="buy", filled_qty=10, filled_avg_price=180.0)
    broker = StubBroker(make_account(positions=[
        PositionView(symbol="AAPL", qty=10, avg_entry_price=180.0,
                     market_value=1800.0, unrealized_pl=0.0)
    ]), orders=[order])

    sync_fills(config, journal, broker)
    # Reset watermark so the same order is seen again; it must not double-record.
    journal.set_state("last_fill_sync", "2000-01-01T00:00:00+00:00")
    report2 = sync_fills(config, journal, broker)
    assert report2.fills_recorded == 0
    assert len(journal.open_lots("AAPL")) == 1


def test_same_day_round_trip_flags_day_trade(tmp_path):
    config = make_config()
    journal = Journal(tmp_path / "j.db")
    now = datetime.now(timezone.utc)
    buy = StubOrder(id="b", symbol="AAPL", side="buy", filled_qty=10,
                    filled_avg_price=180.0, updated_at=now)
    sell = StubOrder(id="s", symbol="AAPL", side="sell", filled_qty=10,
                     filled_avg_price=182.0, updated_at=now)
    broker = StubBroker(make_account(), orders=[buy, sell])

    report = sync_fills(config, journal, broker)
    assert report.day_trades_flagged == 1
    assert journal.day_trades_last_n_days(5) == 1


def test_hifo_closes_highest_cost_lot(tmp_path):
    config = make_config()
    journal = Journal(tmp_path / "j.db")
    # Seed two lots at different cost; a sell should close the higher-cost one.
    low = journal.open_lot(symbol="AAPL", qty=10, price=150.0, ts="2020-01-01T00:00:00+00:00")
    high = journal.open_lot(symbol="AAPL", qty=10, price=200.0, ts="2020-01-02T00:00:00+00:00")
    sell = StubOrder(id="s", symbol="AAPL", side="sell", filled_qty=10, filled_avg_price=190.0)
    broker = StubBroker(make_account(), orders=[sell])

    sync_fills(config, journal, broker)
    open_ids = {lot["id"] for lot in journal.open_lots("AAPL")}
    assert high not in open_ids  # highest-cost lot closed first
    assert low in open_ids


def test_reconciliation_warns_on_drift(tmp_path):
    config = make_config()
    journal = Journal(tmp_path / "j.db")
    # Broker shows 5 shares; journal will have none -> drift warning.
    broker = StubBroker(make_account(positions=[
        PositionView(symbol="MSFT", qty=5, avg_entry_price=400.0,
                     market_value=2000.0, unrealized_pl=0.0)
    ]), orders=[])
    report = sync_fills(config, journal, broker)
    assert any("MSFT" in w for w in report.reconciliation_warnings)


def test_fill_recorded_when_watermark_past_submission(tmp_path):
    """Regression (day-one AAPL): watermark advanced past submitted_at must not
    hide a fill that lands later. Rolling lookback + Alpaca-style submitted_at
    filter must still see the order."""
    config = make_config()
    journal = Journal(tmp_path / "j.db")
    submitted = datetime.now(timezone.utc) - timedelta(minutes=5)
    filled_at = submitted + timedelta(seconds=12)
    # Simulate the bug: watermark sits between submission and fill.
    journal.set_state("last_fill_sync", (submitted + timedelta(seconds=1)).isoformat())
    order = StubOrder(
        id="aapl-day1", symbol="AAPL", side="buy", filled_qty=15,
        filled_avg_price=324.74, submitted_at=submitted, updated_at=filled_at,
        client_order_id="prop-5",
    )
    broker = StubBroker(make_account(positions=[
        PositionView(symbol="AAPL", qty=15, avg_entry_price=324.74,
                     market_value=4871.1, unrealized_pl=-3.6)
    ]), orders=[order])

    report = sync_fills(config, journal, broker)
    assert report.fills_recorded == 1
    assert report.lots_opened == 1
    assert len(journal.open_lots("AAPL")) == 1
    # Halt should clear once journal matches broker.
    assert not journal.get_state("reconcile_halt")

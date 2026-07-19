"""M12: smart limit placement, order slicing, realized slippage + fill quality."""

import pytest

from conftest import make_config

from stubs import StubBroker, StubOrder, make_account

from trading import execution
from trading.broker.sync import sync_fills
from trading.data.journal import Journal


class TestLimitPlacement:
    def test_midpoint(self):
        assert execution.limit_in_spread(99.0, 101.0, "buy", 0.5) == 100.0
        assert execution.limit_in_spread(99.0, 101.0, "sell", 0.5) == 100.0

    def test_passive_vs_aggressive_buy(self):
        assert execution.limit_in_spread(99.0, 101.0, "buy", 0.0) == 99.0   # join bid
        assert execution.limit_in_spread(99.0, 101.0, "buy", 1.0) == 101.0  # cross to ask

    def test_missing_side_falls_back(self):
        assert execution.limit_in_spread(0, 101.0, "buy") == 101.0


class TestSlippage:
    def test_buy_paying_up_is_positive(self):
        # reference 100, filled 100.5 on a buy -> +50 bps adverse
        assert execution.realized_slippage_bps(100.0, 100.5, "buy") == pytest.approx(50.0)

    def test_buy_improvement_is_negative(self):
        assert execution.realized_slippage_bps(100.0, 99.5, "buy") == pytest.approx(-50.0)

    def test_sell_direction(self):
        # sold below reference -> adverse (positive)
        assert execution.realized_slippage_bps(100.0, 99.5, "sell") == pytest.approx(50.0)


class TestSlicing:
    def test_small_order_single_slice(self):
        plan = execution.plan_slices(100, avg_daily_volume=100000)
        assert plan.n == 1
        assert plan.total == 100

    def test_large_order_split_by_participation(self):
        # 30k shares, ADV 100k, 10% participation -> 10k/slice -> 3 slices
        plan = execution.plan_slices(30000, avg_daily_volume=100000, max_participation=0.1)
        assert plan.n == 3
        assert plan.total == pytest.approx(30000)

    def test_capped_slice_count(self):
        plan = execution.plan_slices(10_000_000, avg_daily_volume=100000,
                                     max_participation=0.1, max_slices=10)
        assert plan.n == 10


class TestFillQualityFromSync:
    def test_slippage_recorded_and_reported(self, tmp_path):
        config = make_config()
        journal = Journal(tmp_path / "j.db")
        # A submitted proposal with a reference limit of 100.
        pid = journal.record_proposal(
            agent="strategy", strategy_tag="t", symbol="AAPL", asset_class="stock",
            side="buy", qty=10, order_type="limit", limit_price=100.0)
        journal.set_proposal_status(pid, "submitted")
        # Filled at 100.5 -> +50 bps slippage.
        order = StubOrder(id="o1", symbol="AAPL", side="buy", filled_qty=10,
                          filled_avg_price=100.5, client_order_id=f"prop-{pid}")
        sync_fills(config, journal, StubBroker(make_account(), orders=[order]))

        assert journal.recorded_slippage() == [pytest.approx(50.0)]
        q = execution.fill_quality_report(journal)
        assert q.fills == 1
        assert q.avg_slippage_bps == pytest.approx(50.0)
        assert q.suggested_cost_hurdle_bps == pytest.approx(50.0)  # from adverse fills

    def test_no_fills_uses_floor(self, tmp_path):
        journal = Journal(tmp_path / "j.db")
        q = execution.fill_quality_report(journal, floor_bps=1.0)
        assert q.fills == 0
        assert q.suggested_cost_hurdle_bps == 1.0

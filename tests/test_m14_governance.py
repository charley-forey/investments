"""M14: capital allocation, attribution, and the human-gated live-scaling ladder."""

import pytest

from conftest import make_config, make_quote

from stubs import StubBroker, make_account

from trading.analytics import scaling
from trading.analytics.allocation import (
    allocate_capital, allocation_weight, attribution_report, persist_allocations,
)
from trading.analytics.scorer import score_closed_trades
from trading.data.journal import Journal
from trading.guardrails.engine import OrderPipeline
from trading.guardrails.models import OrderProposal


def seed(journal, tag, open_price, close_price, n=1, days=1):
    from datetime import datetime, timedelta, timezone
    for _ in range(n):
        ts = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        lot = journal.open_lot(symbol="X", qty=10, price=open_price, ts=ts, strategy_tag=tag)
        journal.close_lot(lot, price=close_price)


class TestAllocation:
    def test_winners_get_weight_losers_zero(self, tmp_path):
        journal = Journal(tmp_path / "j.db")
        rates = make_config().settings.tax
        seed(journal, "winner", 100, 110, n=40)   # consistent gains
        seed(journal, "loser", 100, 90, n=40)     # consistent losses
        score_closed_trades(journal)
        allocs = {a.tag: a.weight for a in allocate_capital(journal, rates)}
        assert allocs["winner"] > 0
        assert allocs["loser"] == 0.0

    def test_weights_normalize(self, tmp_path):
        journal = Journal(tmp_path / "j.db")
        rates = make_config().settings.tax
        seed(journal, "a", 100, 110, n=40)
        seed(journal, "b", 100, 105, n=40)
        score_closed_trades(journal)
        allocs = allocate_capital(journal, rates)
        assert sum(a.weight for a in allocs) == pytest.approx(1.0, abs=1e-6)

    def test_confidence_haircut_for_few_trades(self, tmp_path):
        journal = Journal(tmp_path / "j.db")
        rates = make_config().settings.tax
        seed(journal, "fresh", 100, 110, n=3)  # tiny sample
        score_closed_trades(journal)
        a = allocate_capital(journal, rates)[0]
        assert a.confidence < 1.0

    def test_persist_and_read_weight(self, tmp_path):
        journal = Journal(tmp_path / "j.db")
        rates = make_config().settings.tax
        seed(journal, "w", 100, 110, n=40)
        score_closed_trades(journal)
        persist_allocations(journal, allocate_capital(journal, rates))
        assert allocation_weight(journal, "w") > 0

    def test_attribution_shares(self, tmp_path):
        journal = Journal(tmp_path / "j.db")
        rates = make_config().settings.tax
        seed(journal, "a", 100, 110, n=10)
        seed(journal, "b", 100, 120, n=10)
        score_closed_trades(journal)
        rows = attribution_report(journal, rates)
        assert abs(sum(r.share for r in rows) - 1.0) < 1e-6
        assert rows[0].tag == "b"  # bigger contributor first


class TestScalingLadder:
    def test_default_level_zero_multiplier_one(self, tmp_path):
        journal = Journal(tmp_path / "j.db")
        assert scaling.current_level(journal) == 0
        assert scaling.multiplier(journal) == 1.0

    def test_cannot_raise_without_track_record(self, tmp_path):
        journal = Journal(tmp_path / "j.db")
        rates = make_config().settings.tax
        e = scaling.approve_level(journal, rates, 1)  # no trades yet
        assert not e.eligible
        assert scaling.current_level(journal) == 0  # unchanged

    def test_raise_when_eligible(self, tmp_path):
        journal = Journal(tmp_path / "j.db")
        rates = make_config().settings.tax
        seed(journal, "w", 100, 110, n=40)  # 40 trades, profitable
        score_closed_trades(journal)
        e = scaling.approve_level(journal, rates, 1)
        assert e.eligible
        assert scaling.current_level(journal) == 1
        assert scaling.multiplier(journal) == 1.5

    def test_demotion_always_allowed(self, tmp_path):
        journal = Journal(tmp_path / "j.db")
        journal.set_state("live_scale_level", "2")
        scaling.approve_level(journal, make_config().settings.tax, 0)  # demote
        assert scaling.current_level(journal) == 0

    def test_multiplier_applied_to_live_sizing(self, tmp_path):
        from trading.analytics import lifecycle

        config = make_config(mode="live")
        config.limits.live.approval_required = False
        journal = Journal(tmp_path / "j.db")
        lifecycle.set_stage(journal, "t", "scaled")  # stage frac 1.0
        journal.set_state("live_scale_level", "0")   # multiplier 1.0
        pipeline = OrderPipeline(config, journal, StubBroker(make_account(equity=50000)))
        # scaled stage, level 0 -> pos cap = 10% of 50k = 5000. A $6000 order breaches.
        prop = OrderProposal(symbol="AAPL", side="buy", qty=60, order_type="limit",
                             limit_price=100.0, stop_price=95.0, expected_edge_usd=100.0,
                             strategy_tag="t")
        res = pipeline.process(prop, make_account(equity=50000), make_quote(),
                               market_is_open=True)
        assert "max_position" in {v.rule for v in res.result.violations}

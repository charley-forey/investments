"""Tests for counterfactual outcomes, calibration, and proposal_id linkage."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest
from conftest import make_config

from stubs import StubBroker, StubOrder, make_account

from trading.analytics.calibration import build_calibration_report
from trading.analytics.counterfactuals import evaluate_pending, evaluate_proposal
from trading.analytics.scorer import score_closed_trades
from trading.broker.sync import sync_fills
from trading.data.journal import Journal


def _bars(start: str, closes: list[float]):
    """Build a minimal OHLCV DataFrame with dates after `start`."""
    base = datetime.fromisoformat(start.replace("Z", "+00:00")).date()
    idx = [base + timedelta(days=i + 1) for i in range(len(closes))]
    rows = []
    for c in closes:
        rows.append({"open": c, "high": c + 2, "low": c - 2, "close": c, "volume": 1e6})
    return pd.DataFrame(rows, index=pd.DatetimeIndex(idx, name="date"))


def test_evaluate_proposal_stop_hit():
    prop = {"qty": 10, "side": "buy", "limit_price": 100.0, "stop_price": 95.0,
            "expected_edge_usd": 50.0, "status": "vetoed"}
    bars = [
        {"date": "2026-01-02", "open": 100, "high": 101, "low": 94, "close": 96},
    ]
    out = evaluate_proposal(prop, bars)
    assert out is not None
    assert out["stop_hit"] is True
    assert out["hypothetical_pnl"] == pytest.approx(-50.0)
    assert out["verdict_was_right"] is True  # veto correctly avoided a loser


def test_evaluate_proposal_missed_winner():
    prop = {"qty": 10, "side": "buy", "limit_price": 100.0, "stop_price": 95.0,
            "expected_edge_usd": 50.0, "status": "vetoed"}
    # Target = 100 + 5 = 105 from $50 edge / 10 shares
    bars = [
        {"date": "2026-01-02", "open": 100, "high": 106, "low": 99, "close": 105},
    ]
    out = evaluate_proposal(prop, bars)
    assert out is not None
    assert out["target_hit"] is True
    assert out["hypothetical_pnl"] == pytest.approx(50.0)
    assert out["verdict_was_right"] is False  # veto missed a winner


def test_evaluate_pending_records_outcome(tmp_path):
    config = make_config()
    journal = Journal(tmp_path / "j.db")
    old = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    pid = journal.record_proposal(
        agent="strategy", symbol="AAPL", asset_class="stock", side="buy",
        qty=10, order_type="limit", strategy_tag="trend", limit_price=100.0,
        stop_price=95.0, thesis="test", expected_edge_usd=50.0, confidence=0.5,
    )
    journal.conn.execute("UPDATE proposals SET ts=? WHERE id=?", (old, pid))
    journal.conn.commit()
    journal.set_proposal_status(pid, "vetoed")

    class BarsBroker(StubBroker):
        def get_bars(self, symbol, days=30):
            return _bars(old, [100, 98, 94, 93, 92])

    broker = BarsBroker(make_account())
    report = evaluate_pending(journal, broker, min_age_days=5, horizon_days=5)
    assert report.evaluated == 1
    assert journal.has_proposal_outcome(pid)
    outcome = journal.all_proposal_outcomes()[0]
    assert outcome["stop_hit"] == 1
    assert outcome["verdict_was_right"] == 1


def test_sync_propagates_proposal_id_to_lot(tmp_path):
    config = make_config()
    journal = Journal(tmp_path / "j.db")
    pid = journal.record_proposal(
        agent="strategy", symbol="AAPL", asset_class="stock", side="buy",
        qty=10, order_type="limit", strategy_tag="trend", limit_price=180.0,
        stop_price=175.0, thesis="t", expected_edge_usd=50.0,
    )
    journal.set_proposal_status(pid, "submitted")
    order = StubOrder(
        id="o1", symbol="AAPL", side="buy", filled_qty=10,
        filled_avg_price=180.0, client_order_id=f"prop-{pid}",
    )
    broker = StubBroker(make_account(), orders=[order])
    sync_fills(config, journal, broker)
    lot = journal.open_lots("AAPL")[0]
    assert lot["proposal_id"] == pid
    # Close and score — score row must carry proposal_id.
    journal.close_lot(lot["id"], price=190.0)
    score_closed_trades(journal)
    scores = journal.all_scores()
    assert len(scores) == 1
    assert scores[0]["proposal_id"] == pid


def test_calibration_report_builds(tmp_path):
    journal = Journal(tmp_path / "j.db")
    pid = journal.record_proposal(
        agent="strategy", symbol="AAPL", asset_class="stock", side="buy",
        qty=10, order_type="limit", strategy_tag="trend", limit_price=100.0,
        stop_price=95.0, thesis="t", expected_edge_usd=50.0, confidence=0.55,
    )
    journal.set_proposal_status(pid, "vetoed")
    journal.record_verdict(pid, source="guardrail", verdict="reject",
                           rule="max_position", reason="too big")
    journal.record_proposal_outcome(
        proposal_id=pid, horizon_days=5, entry_price=100.0, stop_price=95.0,
        target_price=105.0, max_favorable_usd=10.0, max_adverse_usd=50.0,
        hypothetical_pnl=-50.0, stop_hit=True, verdict_was_right=True,
    )
    report = build_calibration_report(journal)
    assert report.n_proposals == 1
    assert report.n_outcomes == 1
    assert report.veto_hit_rate == 1.0
    assert report.guardrail_reasons.get("max_position") == 1
    assert "Calibration" in report.text

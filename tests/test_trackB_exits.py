"""Track B: deterministic exit rules. Each rule tested in isolation + edge cases."""

from datetime import date, timedelta

from trading.broker.models import PositionView, Quote
from trading.broker.occ import build_occ
from trading.analytics.exits import ExitRules, evaluate_exits, pnl_pct


def _pos(symbol="AAPL", qty=100, entry=100.0, mv=None, upl=0.0, asset_class="stock"):
    mv = mv if mv is not None else qty * entry
    return PositionView(symbol=symbol, qty=qty, avg_entry_price=entry,
                        market_value=mv, unrealized_pl=upl, asset_class=asset_class)


def test_hard_stop_fires_on_loss():
    pos = _pos(entry=100.0)
    acts = evaluate_exits([pos], {"AAPL": 90.0}, rules=ExitRules(stop_loss_pct=8.0))
    assert len(acts) == 1 and acts[0].action == "close" and acts[0].urgency == "high"
    assert "stop" in acts[0].reason


def test_hard_stop_quiet_when_above_threshold():
    pos = _pos(entry=100.0)
    acts = evaluate_exits([pos], {"AAPL": 95.0}, rules=ExitRules(stop_loss_pct=8.0))
    assert acts == []


def test_no_stop_set_means_no_action():
    pos = _pos(entry=100.0)
    acts = evaluate_exits([pos], {"AAPL": 50.0}, rules=ExitRules())
    assert acts == []


def test_short_position_stop_uses_direction():
    # short at 100, price rises to 110 -> -10% for the short.
    pos = _pos(qty=-100, entry=100.0)
    acts = evaluate_exits([pos], {"AAPL": 110.0}, rules=ExitRules(stop_loss_pct=8.0))
    assert len(acts) == 1 and acts[0].action == "close"


def test_take_profit_fires():
    pos = _pos(entry=100.0)
    acts = evaluate_exits([pos], {"AAPL": 130.0}, rules=ExitRules(take_profit_pct=25.0))
    assert len(acts) == 1 and "profit" in acts[0].reason and acts[0].urgency == "normal"


def test_trailing_stop_fires_off_peak():
    pos = _pos(entry=100.0)
    rules = ExitRules(trailing_pct=10.0, high_water={"AAPL": 120.0})
    acts = evaluate_exits([pos], {"AAPL": 105.0}, rules=rules)  # 12.5% off peak
    assert len(acts) == 1 and acts[0].urgency == "high" and "trailing" in acts[0].reason


def test_trailing_stop_quiet_within_band():
    pos = _pos(entry=100.0)
    rules = ExitRules(trailing_pct=10.0, high_water={"AAPL": 120.0})
    acts = evaluate_exits([pos], {"AAPL": 115.0}, rules=rules)  # 4% off peak
    assert acts == []


def test_trailing_needs_high_water():
    pos = _pos(entry=100.0)
    acts = evaluate_exits([pos], {"AAPL": 50.0}, rules=ExitRules(trailing_pct=10.0))
    assert acts == []  # no hwm provided -> rule inert


def test_time_exit_fires():
    pos = _pos(entry=100.0)
    opened = date(2026, 1, 1)
    rules = ExitRules(max_holding_days=30, open_dates={"AAPL": opened},
                      as_of=opened + timedelta(days=45))
    acts = evaluate_exits([pos], {"AAPL": 100.0}, rules=rules)
    assert len(acts) == 1 and acts[0].urgency == "low" and "time" in acts[0].reason


def test_time_exit_quiet_when_young():
    pos = _pos(entry=100.0)
    opened = date(2026, 1, 1)
    rules = ExitRules(max_holding_days=30, open_dates={"AAPL": opened},
                      as_of=opened + timedelta(days=10))
    assert evaluate_exits([pos], {"AAPL": 100.0}, rules=rules) == []


def test_option_expiry_roll():
    as_of = date(2026, 7, 20)
    occ = build_occ("AAPL", as_of + timedelta(days=3), "call", 190.0)
    pos = _pos(symbol=occ, qty=1, entry=5.0, asset_class="option")
    rules = ExitRules(option_roll_dte=7, as_of=as_of)
    acts = evaluate_exits([pos], {occ: 5.0}, rules=rules)
    assert len(acts) == 1 and acts[0].action == "roll" and "expiry" in acts[0].reason


def test_option_roll_urgency_high_near_expiry():
    as_of = date(2026, 7, 20)
    occ = build_occ("AAPL", as_of + timedelta(days=1), "put", 100.0)
    pos = _pos(symbol=occ, qty=1, entry=2.0, asset_class="option")
    acts = evaluate_exits([pos], {occ: 2.0}, rules=ExitRules(option_roll_dte=7, as_of=as_of))
    assert acts[0].urgency == "high"


def test_stock_ignores_roll_rule():
    pos = _pos(entry=100.0)
    acts = evaluate_exits([pos], {"AAPL": 100.0}, rules=ExitRules(option_roll_dte=7))
    assert acts == []


def test_missing_mark_falls_back_to_unrealized_pl():
    # No mark for AAPL; -10% embedded in the position view (cost 10000, upl -1000).
    pos = _pos(entry=100.0, mv=9000.0, upl=-1000.0)
    acts = evaluate_exits([pos], {}, rules=ExitRules(stop_loss_pct=8.0))
    assert len(acts) == 1 and acts[0].action == "close"


def test_stop_beats_take_profit_priority():
    # Contrived: both thresholds set, loss present -> stop wins (only one action).
    pos = _pos(entry=100.0)
    rules = ExitRules(stop_loss_pct=8.0, take_profit_pct=25.0)
    acts = evaluate_exits([pos], {"AAPL": 85.0}, rules=rules)
    assert len(acts) == 1 and "stop" in acts[0].reason


def test_bad_position_does_not_raise():
    class Broken:
        symbol = "X"
        # missing attrs -> getattr defaults / caught
    acts = evaluate_exits([Broken()], {"X": 10.0}, rules=ExitRules(stop_loss_pct=8.0))
    assert isinstance(acts, list)


def test_quote_mark_supported():
    pos = _pos(entry=100.0)
    q = Quote(symbol="AAPL", bid=89.0, ask=91.0)  # mid 90 -> -10%
    acts = evaluate_exits([pos], {"AAPL": q}, rules=ExitRules(stop_loss_pct=8.0))
    assert len(acts) == 1


def test_pnl_pct_helper():
    assert round(pnl_pct(_pos(qty=100, entry=100.0), 110.0), 4) == 10.0
    assert round(pnl_pct(_pos(qty=-100, entry=100.0), 110.0), 4) == -10.0
    assert pnl_pct(_pos(entry=0.0), None) is None


if __name__ == "__main__":
    import sys
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))

"""Track A: intraday signal depth — intraday features, calendar seam, risk regime,
and options term structure. Synthetic inputs only; no network."""

from __future__ import annotations

import json
from datetime import date, timedelta

from trading.analytics.intraday import compute_intraday_features
from trading.analytics.regime import assess_regime
from trading.analytics.options_term import term_structure, iv_term_slope, skew_by_expiry
from trading.analytics.options import ContractRow
from trading.data.calendar_provider import JsonCalendarProvider, get_calendar_provider


class _Bar:
    def __init__(self, o, h, l, c, v):
        self.open, self.high, self.low, self.close, self.volume = o, h, l, c, v


# -- intraday -----------------------------------------------------------------

def test_intraday_insufficient_bars_returns_none():
    bars = [_Bar(100, 101, 99, 100, 1000) for _ in range(3)]
    assert compute_intraday_features(bars, or_bars=5) is None
    assert compute_intraday_features([], or_bars=5) is None
    assert compute_intraday_features(None) is None


def test_intraday_breakout_up():
    # First 5 bars set an OR of ~99-101; price then climbs to 105 on a volume spike.
    bars = [_Bar(100, 101, 99, 100, 1000) for _ in range(5)]
    bars += [_Bar(101, 103, 100, 102, 1200), _Bar(102, 105, 101, 105, 5000)]
    f = compute_intraday_features(bars, or_bars=5)
    assert f is not None
    assert f.opening_range_high == 101.0
    assert f.opening_range_low == 99.0
    assert f.or_breakout_pct > 0          # last (105) above OR high (101)
    assert f.intraday_momentum > 0        # 105 vs first open 100
    assert f.relative_volume > 1          # 5000 vs prior avg
    # VWAP sits between the low and high of the session.
    assert f.opening_range_low <= f.vwap <= 105
    assert "vwap" in f.summary()


def test_intraday_vwap_falls_back_to_mean_without_volume():
    bars = [_Bar(10, 10, 10, 10, 0) for _ in range(6)]
    f = compute_intraday_features(bars, or_bars=5)
    assert f is not None
    assert f.vwap == 10.0
    assert f.relative_volume == 0.0       # no volume -> 0, not a crash


# -- calendar provider --------------------------------------------------------

class _Cfg:
    def __init__(self, path):
        self.settings = type("S", (), {"paths": type("P", (), {"calendar_file": str(path)})()})()


def test_calendar_missing_file_degrades_to_empty(tmp_path):
    p = JsonCalendarProvider(tmp_path / "nope.json")
    assert p.upcoming_events("AAPL") == []


def test_calendar_reads_and_filters(tmp_path):
    path = tmp_path / "calendar.json"
    soon = (date.today() + timedelta(days=3)).isoformat()
    far = (date.today() + timedelta(days=90)).isoformat()
    past = (date.today() - timedelta(days=2)).isoformat()
    path.write_text(json.dumps([
        {"symbol": "AAPL", "date": soon, "kind": "earnings", "title": "Q3 earnings"},
        {"symbol": "AAPL", "date": far, "kind": "earnings", "title": "far out"},
        {"symbol": "AAPL", "date": past, "kind": "earnings", "title": "already happened"},
        {"symbol": "MSFT", "date": soon, "kind": "earnings", "title": "other symbol"},
        {"symbol": "SPY", "date": soon, "event": "CPI"},  # legacy 'event' key
    ]), encoding="utf-8")

    p = JsonCalendarProvider(path)
    ev = p.upcoming_events("AAPL", days=14)
    assert len(ev) == 1
    assert ev[0].title == "Q3 earnings" and ev[0].kind == "earnings"

    # No symbol filter -> all upcoming within horizon (AAPL soon, MSFT, SPY).
    allev = p.upcoming_events(days=14)
    assert {e.symbol for e in allev} == {"AAPL", "MSFT", "SPY"}
    spy = [e for e in allev if e.symbol == "SPY"][0]
    assert spy.title == "CPI" and spy.kind == "CPI"   # legacy alias fills both

    # Factory wires the configured path.
    assert isinstance(get_calendar_provider(_Cfg(path)), JsonCalendarProvider)


# -- regime -------------------------------------------------------------------

def test_regime_risk_off():
    r = assess_regime(spy_trend="down", spy_vol=0.35, vix_level=32, breadth_pct=25)
    assert r.label == "risk-off"
    assert r.risk_score >= 60
    assert r.drivers and "VIX" in r.summary()


def test_regime_risk_on():
    r = assess_regime(spy_trend="up", spy_vol=0.10, vix_level=13, breadth_pct=70)
    assert r.label == "risk-on"
    assert r.risk_score <= 40


def test_regime_partial_and_empty_inputs():
    # Only VIX supplied -> score is just the VIX signal.
    r = assess_regime(vix_level=32)
    assert r.risk_score > 60 and len(r.drivers) == 1
    # Nothing supplied -> unknown, no crash.
    assert assess_regime().label == "unknown"


# -- options term structure ---------------------------------------------------

def _row(expiry, right, strike, spot, iv, dte):
    return ContractRow(
        occ=f"X{expiry}{right}{strike}", expiry=expiry, right=right, strike=strike,
        dte=dte, moneyness=(strike - spot) / spot, bid=1.0, ask=1.1, mid=1.05,
        spread_bps=95.0, iv=iv, delta=None, gamma=None, theta=None, vega=None,
        last_size=None,
    )


def test_term_structure_and_slope_and_skew():
    spot = 100.0
    e1 = date.today() + timedelta(days=7)
    e2 = date.today() + timedelta(days=30)
    rows = [
        # front expiry: ATM ~100, put IV richer than call (fear)
        _row(e1, "call", 100, spot, 0.30, 7),
        _row(e1, "put", 100, spot, 0.36, 7),
        _row(e1, "call", 110, spot, 0.28, 7),   # further OTM, should be ignored for ATM
        # back expiry: higher IV (contango)
        _row(e2, "call", 100, spot, 0.34, 30),
        _row(e2, "put", 100, spot, 0.35, 30),
    ]
    ts = term_structure(rows)
    assert [p.expiry for p in ts] == [e1, e2]          # sorted by expiry
    # front ATM IV averages the nearest-money contract (call 100 = 0.30 wins over 110)
    assert ts[0].atm_iv == 0.30
    assert ts[1].atm_iv == 0.34

    slope = iv_term_slope(rows)
    assert slope is not None and slope > 0             # back > front => contango

    skew = skew_by_expiry(rows)
    assert len(skew) == 2
    front = [s for s in skew if s.expiry == e1][0]
    assert round(front.skew, 4) == round(0.36 - 0.30, 4)   # put - call > 0


def test_term_structure_degrades_without_iv():
    e1 = date.today() + timedelta(days=7)
    rows = [_row(e1, "call", 100, 100.0, None, 7)]
    assert term_structure(rows) == []
    assert iv_term_slope(rows) is None
    assert skew_by_expiry(rows) == []

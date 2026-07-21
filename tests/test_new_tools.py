"""New agent tools: scan_universe, fundamentals, open_orders, calendar macros, VIX."""

from __future__ import annotations

import json
from datetime import date, timedelta

import pandas as pd

from conftest import make_config
from stubs import StubBroker, make_account

from trading.data.calendar_feed import _merge_events, refresh_calendar
from trading.data.fundamentals import Fundamentals, FundamentalsCache, get_fundamentals
from trading.data.journal import Journal
from trading.data.macro_calendar import macro_event_dicts
from trading.tools.market_context import compute_regime
from trading.tools.registry import STRATEGY_TOOLS, ToolContext, ToolRegistry


def _bars_df(n: int = 60, start: float = 100.0):
    """Synthetic OHLCV rising then flat."""
    rows = []
    price = start
    idx = []
    for i in range(n):
        o = price
        c = price * (1.01 if i < 40 else 1.0)
        h = max(o, c) * 1.005
        l = min(o, c) * 0.995
        rows.append({"open": o, "high": h, "low": l, "close": c, "volume": 1_000_000 + i * 1000})
        idx.append(pd.Timestamp("2026-01-01") + pd.Timedelta(days=i))
        price = c
    return pd.DataFrame(rows, index=idx)


class RichStubBroker(StubBroker):
    def __init__(self, account, bars_by_sym=None, open_orders=None):
        super().__init__(account)
        self._bars = bars_by_sym or {}
        self._open_orders = open_orders or []

    def get_bars(self, symbol, days=30, timeframe="1Day"):
        df = self._bars.get(symbol.upper())
        if df is None:
            return None
        return df.tail(days)


def _ctx(tmp_path, broker=None, universe=None):
    config = make_config()
    if universe:
        config.settings.universe.core = universe
    config.settings.paths.fundamentals_db = str(tmp_path / "fund.db")
    config.settings.paths.calendar_file = str(tmp_path / "calendar.json")
    journal = Journal(tmp_path / "j.db")
    account = make_account()
    broker = broker or RichStubBroker(account)
    return ToolContext(config=config, journal=journal, broker=broker,
                       account_state=account, agent_name="strategy"), config


def test_scan_universe_ranks_symbols(tmp_path):
    spy = _bars_df(60, 400)
    aapl = _bars_df(60, 100)
    # Spike AAPL's recent closes so momentum ranks higher than SPY
    for i in range(-10, 0):
        aapl.iloc[i, aapl.columns.get_loc("close")] = float(aapl.iloc[i - 1]["close"]) * 1.03
        aapl.iloc[i, aapl.columns.get_loc("open")] = float(aapl.iloc[i]["close"]) * 0.99
        aapl.iloc[i, aapl.columns.get_loc("high")] = float(aapl.iloc[i]["close"]) * 1.01
        aapl.iloc[i, aapl.columns.get_loc("low")] = float(aapl.iloc[i]["close"]) * 0.98
    broker = RichStubBroker(make_account(), bars_by_sym={"AAPL": aapl, "SPY": spy})
    ctx, _ = _ctx(tmp_path, broker=broker, universe=["AAPL", "SPY"])
    reg = ToolRegistry(ctx, STRATEGY_TOOLS)
    out = reg.dispatch("scan_universe", {"sort_by": "momentum"})
    assert "universe scan" in out
    assert "AAPL" in out and "SPY" in out
    assert out.index("AAPL") < out.index("SPY")


def test_get_open_orders(tmp_path):
    class O:
        id = "ord-1"
        symbol = "AAPL"
        side = "buy"
        qty = 10
        order_type = "limit"
        limit_price = 180.0
        status = "new"

    broker = RichStubBroker(make_account(), open_orders=[O()])
    ctx, _ = _ctx(tmp_path, broker=broker)
    out = ToolRegistry(ctx, STRATEGY_TOOLS).dispatch("get_open_orders", {})
    assert "AAPL" in out and "180" in out


def test_get_open_orders_empty(tmp_path):
    ctx, _ = _ctx(tmp_path)
    out = ToolRegistry(ctx, STRATEGY_TOOLS).dispatch("get_open_orders", {})
    assert "no open" in out


def test_fundamentals_cache_and_tool(tmp_path):
    ctx, config = _ctx(tmp_path)

    def fake_fetch(symbol):
        return Fundamentals(
            symbol=symbol.upper(), market_cap=1e12, trailing_pe=25.0,
            forward_pe=22.0, revenue_growth=0.12, profit_margin=0.25,
            short_pct_float=0.01, beta=1.1, next_earnings="2026-08-01",
        )

    feats = get_fundamentals(config, "AAPL", fetch_fn=fake_fetch)
    assert feats.market_cap == 1e12
    # Second call hits cache (fake_fetch would still work, but cache is used)
    cache = FundamentalsCache(config.settings.paths.fundamentals_db)
    assert cache.get("AAPL") is not None
    cache.close()

    # Tool path: monkey via get_fundamentals already cached
    out = ToolRegistry(ctx, STRATEGY_TOOLS).dispatch("get_fundamentals", {"symbol": "AAPL"})
    assert "AAPL" in out and "P/E" in out


def test_macro_events_merge_into_calendar(tmp_path):
    config = make_config()
    config.settings.paths.calendar_file = str(tmp_path / "cal.json")
    config.settings.universe.core = ["AAPL"]

    def fake_earn(sym):
        return [{"date": "2026-08-15", "symbol": sym, "event": "earnings"}]

    report = refresh_calendar(config, fetch_fn=fake_earn)
    assert report.events_written > 1
    data = json.loads((tmp_path / "cal.json").read_text(encoding="utf-8"))
    assert any(e.get("event") == "earnings" for e in data)
    assert any(e.get("event_type") == "macro" for e in data)
    assert any(e.get("event") == "FOMC" for e in data)


def test_get_calendar_includes_macros(tmp_path):
    ctx, config = _ctx(tmp_path)
    today = date.today()
    events = [
        {"date": (today + timedelta(days=2)).isoformat(), "symbol": "AAPL",
         "event": "earnings", "event_type": "earnings"},
        {"date": (today + timedelta(days=3)).isoformat(), "symbol": "",
         "event": "CPI", "event_type": "macro"},
    ]
    path = tmp_path / "calendar.json"
    path.write_text(json.dumps(events), encoding="utf-8")
    config.settings.paths.calendar_file = str(path)
    out = ToolRegistry(ctx, STRATEGY_TOOLS).dispatch("get_calendar", {"symbol": "AAPL"})
    assert "AAPL" in out and "earnings" in out
    assert "CPI" in out and "macro" in out


def test_macro_event_dicts_nonempty():
    assert len(macro_event_dicts()) >= 20
    assert all("event_type" in e for e in macro_event_dicts())


def test_merge_preserves_event_type():
    merged = _merge_events(
        [],
        [{"date": "2026-09-01", "symbol": "", "event": "FOMC", "event_type": "macro"}],
    )
    assert merged[0]["event_type"] == "macro"


def test_vix_regime_in_market_context():
    closes = [100 + i * 0.2 for i in range(60)]
    vix = [12.0] * 10 + [14.0] * 10 + [13.0] * 10
    regime = compute_regime(closes, vix_closes=vix)
    assert regime.trend in ("up", "down", "sideways")
    assert regime.vix == 13.0
    assert regime.iv_regime == "cheap"
    summary = regime.summary()
    assert "VIX" in summary and "IV" in summary


def test_new_tools_in_strategy_schemas():
    for name in ("scan_universe", "get_fundamentals", "get_open_orders"):
        assert name in STRATEGY_TOOLS

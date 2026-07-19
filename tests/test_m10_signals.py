"""M10: technical features, pluggable sentiment provider, calendar tool."""

import json

from conftest import make_config

from stubs import StubBroker, make_account

from trading.analytics.features import compute_features
from trading.data import sentiment
from trading.data.journal import Journal
from trading.tools.registry import STRATEGY_TOOLS, ToolContext, ToolRegistry


class FakeBar:
    def __init__(self, o, h, l, c):
        self.open, self.high, self.low, self.close = o, h, l, c


class FakeDF:
    """Minimal iterrows()/len() shim so the features tool path is exercised."""
    def __init__(self, rows):
        self._rows = rows

    def __len__(self):
        return len(self._rows)

    def iterrows(self):
        for i, r in enumerate(self._rows):
            yield i, r


class FeatureBroker(StubBroker):
    def __init__(self, closes):
        super().__init__(make_account())
        self._closes = closes

    def get_bars(self, symbol, days=30):
        return FakeDF([{"open": c, "high": c + 1, "low": c - 1, "close": c}
                       for c in self._closes])


class TestFeatures:
    def test_uptrend_features(self):
        bars = [FakeBar(c, c + 1, c - 1, c) for c in [100 + i for i in range(40)]]
        f = compute_features(bars)
        assert f is not None
        assert f.momentum_20 > 0        # rising
        assert f.sma_gap > 0            # fast above slow
        assert f.dist_from_high == 0.0  # at the highs in a steady uptrend

    def test_insufficient_history(self):
        bars = [FakeBar(c, c, c, c) for c in [100, 101, 102]]
        assert compute_features(bars) is None

    def test_features_tool(self, tmp_path):
        broker = FeatureBroker([100 + i for i in range(50)])
        ctx = ToolContext(config=make_config(), journal=Journal(tmp_path / "j.db"),
                          broker=broker, account_state=make_account())
        reg = ToolRegistry(ctx, STRATEGY_TOOLS)
        out = reg.dispatch("get_features", {"symbol": "AAPL"})
        assert "mom" in out and "vol" in out


class TestSentimentProvider:
    def test_default_is_lexicon(self):
        provider = sentiment.get_provider(make_config())
        assert isinstance(provider, sentiment.LexiconSentimentProvider)

    def test_provider_scores(self):
        class NewsBroker(StubBroker):
            def get_news(self, symbol, limit=10):
                return [{"headline": "AAPL surges to record", "summary": "",
                         "created_at": "2026-01-01", "source": "x"}]
        provider = sentiment.get_provider(make_config())
        sig = provider.score(make_config(), NewsBroker(make_account()), "AAPL")
        assert sig.polarity > 0


class TestCalendarTool:
    def _ctx(self, tmp_path, events=None):
        config = make_config()
        cal = tmp_path / "calendar.json"
        if events is not None:
            cal.write_text(json.dumps(events), encoding="utf-8")
        config.settings.paths.calendar_file = str(cal)
        return ToolContext(config=config, journal=Journal(tmp_path / "j.db"),
                           broker=StubBroker(make_account()), account_state=make_account())

    def test_no_calendar_configured(self, tmp_path):
        reg = ToolRegistry(self._ctx(tmp_path), STRATEGY_TOOLS)
        assert "no calendar feed" in reg.dispatch("get_calendar", {"symbol": "AAPL"})

    def test_returns_upcoming_for_symbol(self, tmp_path):
        events = [
            {"date": "2999-01-15", "symbol": "AAPL", "event": "earnings"},
            {"date": "2999-02-01", "symbol": "MSFT", "event": "earnings"},
            {"date": "2000-01-01", "symbol": "AAPL", "event": "old earnings"},
        ]
        reg = ToolRegistry(self._ctx(tmp_path, events), STRATEGY_TOOLS)
        out = reg.dispatch("get_calendar", {"symbol": "AAPL"})
        assert "2999-01-15" in out
        assert "MSFT" not in out       # filtered to the symbol
        assert "2000-01-01" not in out  # past event excluded

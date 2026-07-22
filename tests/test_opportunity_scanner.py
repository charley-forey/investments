"""Opportunity engine / movers scanner tests."""

from datetime import datetime, timezone

from conftest import make_config
from stubs import StubBroker, make_account

from trading.analytics.opportunity import score_opportunity, suggest_template
from trading.data.journal import Journal
from trading.data.sentiment import SentimentSignal
from trading.scanner.learning import (
    effective_core_symbols, promote_demote_core, retune_weights, stats_by_source,
)
from trading.scanner.movers import candidate_context, load_candidates, run_movers_scan
from trading.scanner.universe import load_screen_universe
from trading.triggers import should_run_intraday_llm


def test_load_screen_universe():
    screen = load_screen_universe()
    assert len(screen.symbols) > 20
    assert screen.filters.top_n >= 1
    assert screen.filters.wake_score > 0


def test_score_opportunity_filters_illiquid():
    opp = score_opportunity(
        symbol="PENNY", last=2.0, day_pct=0.1, gap_pct=0.05, rvol=5.0,
        atr_pct=0.05, momentum=0.2, spread_bps=10.0, min_price=5.0,
    )
    assert opp is None


def test_score_opportunity_ranks_hot_name():
    opp = score_opportunity(
        symbol="HOT", last=50.0, day_pct=0.05, gap_pct=0.02, rvol=3.0,
        atr_pct=0.03, momentum=0.1, spread_bps=5.0, dist20=0.04,
        spy_day_pct=0.0, news_count_24h=4, social_mentions=10,
        min_price=5.0, max_spread_bps=40.0,
    )
    assert opp is not None
    assert opp.score >= 40
    assert opp.template is not None
    assert opp.trigger_level > 0


def test_suggest_template():
    assert suggest_template(0.03, 0.02, 2.0, 0.8, 0.2) == "news-impulse"
    assert suggest_template(0.02, 0.02, 2.0, 0.1, 0.2) == "gap-and-go"
    assert suggest_template(0.01, 0.0, 1.5, 0.1, 0.8) == "orb-breakout"


def test_movers_scan_writes_candidates(tmp_path):
    import pandas as pd

    config = make_config()
    config.settings.paths.journal_db = str(tmp_path / "j.db")
    config.settings.universe.core = ["AAPL", "SPY"]
    journal = Journal(tmp_path / "j.db")

    def _bars(n=40, last=100.0, up=True):
        rows = []
        px = last - n
        for i in range(n):
            px += 1.5 if up else -0.5
            rows.append({
                "open": px - 0.5, "high": px + 1, "low": px - 1,
                "close": px, "volume": 2_000_000 + i * 10_000,
            })
        return pd.DataFrame(rows)

    class ScanBroker(StubBroker):
        def get_bars(self, symbol, days=30, timeframe="1Day"):
            return _bars(up=(symbol != "SPY"))

        def get_quote(self, symbol):
            from trading.broker.models import Quote
            return Quote(symbol=symbol, bid=99.9, ask=100.1)

    # Patch screen universe to only scan core for speed
    import trading.scanner.movers as movers_mod
    from trading.scanner.universe import ScreenFilters, ScreenUniverse

    def _tiny_screen():
        return ScreenUniverse(symbols=["AAPL", "SPY"], filters=ScreenFilters(top_n=2))

    orig = movers_mod.load_screen_universe
    movers_mod.load_screen_universe = _tiny_screen
    try:
        report = run_movers_scan(config, ScanBroker(make_account()), journal=journal)
    finally:
        movers_mod.load_screen_universe = orig

    assert report.candidates >= 1
    cands = load_candidates(config)
    assert cands
    ctx = candidate_context(config)
    assert "AAPL" in ctx or "SPY" in ctx
    assert "OpportunityScore" in ctx or "score=" in ctx


def test_gate_wakes_on_high_score(tmp_path):
    config = make_config()
    config.settings.agents.trigger_gate_enabled = True
    config.settings.paths.journal_db = str(tmp_path / "j.db")
    journal = Journal(tmp_path / "j.db")
    # Write a hot candidate
    path = tmp_path / "candidates.json"
    # candidates_path derives from journal_db parent
    import json
    (tmp_path / "candidates.json").write_text(json.dumps({
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "wake_score": 55,
        "candidates": [{
            "symbol": "HOT", "score": 80, "day_pct": 0.05, "rvol": 3,
            "template": "orb-breakout", "expires_at": "2099-01-01T00:00:00+00:00",
            "trigger_direction": "above", "trigger_level": 100,
        }],
    }), encoding="utf-8")
    from zoneinfo import ZoneInfo
    now = datetime(2026, 7, 21, 11, 0, tzinfo=ZoneInfo("America/New_York"))
    decision = should_run_intraday_llm(
        config, journal, StubBroker(make_account()), make_account(), now_et=now,
    )
    assert decision.run_llm
    assert "opportunity score" in decision.reason


def test_learning_promote_and_stats(tmp_path):
    config = make_config()
    journal = Journal(tmp_path / "j.db")
    # Seed scanner submissions
    for i in range(4):
        pid = journal.record_proposal(
            agent="strategy", symbol="NEWCO", asset_class="stock", side="buy",
            qty=10, order_type="limit", strategy_tag="orb-breakout",
            limit_price=10.0, stop_price=9.0, discovery_source="scanner",
            score_at_entry=70.0,
        )
        journal.set_proposal_status(pid, "submitted")
        journal.record_score(strategy_tag="orb-breakout", pnl_usd=50.0, term="short",
                             proposal_id=pid, grade="A", thesis_adherence=1.0, notes="")
    report = promote_demote_core(config, journal, min_submitted=3)
    assert "NEWCO" in report.promoted
    core = effective_core_symbols(config, journal)
    assert "NEWCO" in core
    text = stats_by_source(journal)
    assert "scanner" in text
    weights = retune_weights(journal)
    assert "rvol" in weights


def test_reddit_posts_on_sentiment_signal():
    sig = SentimentSignal(
        symbol="AAPL", reddit_mentions=1, reddit_score=10, polarity=0.2,
        top_headlines=["AAPL rises"],
        reddit_posts=[{"title": "AAPL moon", "score": 10, "author": "u", "subreddit": "stocks"}],
    )
    assert sig.reddit_posts and sig.reddit_posts[0]["title"] == "AAPL moon"

"""M15: intel store, ingestion, curation, sentiment->outcome research, vectors."""

import pytest

from conftest import make_config

from stubs import ScriptedClient, StubBroker, _Response, make_account, text_block

from trading.analytics.signal_research import is_promising, study_sentiment_signal
from trading.data.intel import IntelStore, NewsItem
from trading.data.journal import Journal
from trading.data.vectors import HashingEmbedder, VectorStore
from trading.tools.registry import STRATEGY_TOOLS, ToolContext, ToolRegistry


class NewsBroker(StubBroker):
    def __init__(self, news_by_symbol):
        super().__init__(make_account())
        self._news = news_by_symbol

    def get_news(self, symbol, limit=10):
        return self._news.get(symbol.upper(), [])


class TestIntelStore:
    def test_news_dedup_and_query(self, tmp_path):
        store = IntelStore(tmp_path / "intel.db")
        items = [NewsItem(symbol="AAPL", headline="AAPL beats", source="x"),
                 NewsItem(symbol="AAPL", headline="AAPL beats", source="x")]  # dup
        assert store.save_news(items) == 1  # deduped by content hash
        assert store.save_news(items) == 0  # idempotent on re-run
        assert len(store.recent_news("AAPL")) == 1

    def test_sentiment_history_and_digest(self, tmp_path):
        store = IntelStore(tmp_path / "intel.db")
        store.record_sentiment(symbol="AAPL", polarity=0.5, mention_count=3)
        assert len(store.sentiment_history("AAPL", days=7)) == 1
        store.save_digest("## Themes\n- AI capex")
        assert "AI capex" in store.latest_digest()["digest_md"]


class TestIngestion:
    def test_ingest_persists_and_is_idempotent(self, tmp_path):
        config = make_config()
        config.settings.universe.core = ["AAPL"]
        store = IntelStore(tmp_path / "intel.db")
        broker = NewsBroker({"AAPL": [
            {"headline": "AAPL surges to record", "summary": "", "created_at": "2026-01-01",
             "source": "x"}]})
        from trading.data.ingest import ingest_intel

        r1 = ingest_intel(config, store, broker)
        assert r1.news_saved == 1
        assert r1.sentiment_snapshots == 1
        r2 = ingest_intel(config, store, broker)
        assert r2.news_saved == 0  # deduped
        assert len(store.recent_news("AAPL")) == 1


class TestCuration:
    def test_digest_generated_and_stored(self, tmp_path):
        config = make_config()
        store = IntelStore(tmp_path / "intel.db")
        store.save_news([NewsItem(symbol="AAPL", headline="AAPL beats", source="x")])
        client = ScriptedClient([_Response([text_block("## Themes\n- Strong earnings")],
                                           "end_turn")])
        from trading.agents.intel import run_intel_session

        digest = run_intel_session(client, config, store)
        assert "Strong earnings" in digest
        assert store.latest_digest()["digest_md"] == digest


class TestIntelTools:
    def test_get_market_intel_tool(self, tmp_path):
        config = make_config()
        intel_path = tmp_path / "intel.db"
        config.settings.paths.intel_db = str(intel_path)
        IntelStore(intel_path).save_digest("## Themes\n- Rotation into energy")
        ctx = ToolContext(config=config, journal=Journal(tmp_path / "j.db"),
                          broker=StubBroker(make_account()), account_state=make_account())
        out = ToolRegistry(ctx, STRATEGY_TOOLS).dispatch("get_market_intel", {})
        assert "energy" in out

    def test_recall_similar_tool(self, tmp_path):
        config = make_config()
        vpath = tmp_path / "vectors.db"
        config.settings.paths.vectors_db = str(vpath)
        vs = VectorStore(vpath)
        vs.add("lesson", "1", "avoid holding through earnings gaps on momentum names")
        vs.add("lesson", "2", "cash-secured puts work in low volatility regimes")
        vs.close()
        ctx = ToolContext(config=config, journal=Journal(tmp_path / "j.db"),
                          broker=StubBroker(make_account()), account_state=make_account())
        out = ToolRegistry(ctx, STRATEGY_TOOLS).dispatch(
            "recall_similar", {"query": "earnings gap risk"})
        assert "earnings" in out


class TestVectors:
    def test_similar_ranks_higher(self):
        emb = HashingEmbedder()
        from trading.data.vectors import cosine
        a = emb.embed("earnings beat drives stock higher")
        b = emb.embed("earnings beat drives stock higher")
        c = emb.embed("weather forecast rain tomorrow")
        assert cosine(a, b) == pytest.approx(1.0, abs=1e-6)
        assert cosine(a, c) < cosine(a, b)

    def test_store_search(self, tmp_path):
        vs = VectorStore(tmp_path / "v.db")
        vs.add("news", "n1", "Fed signals rate cuts, markets rally")
        vs.add("news", "n2", "chip stocks surge on AI demand")
        hits = vs.search("interest rate policy", k=2)
        assert hits[0].ref_id == "n1"

    def test_upsert_dedupes(self, tmp_path):
        vs = VectorStore(tmp_path / "v.db")
        vs.add("lesson", "1", "first")
        vs.add("lesson", "1", "updated")
        assert vs.count() == 1


class TestSignalResearch:
    class Bar:
        def __init__(self, date, close):
            self.date, self.close = date, close

    def test_positive_sentiment_forward_return(self):
        sentiment = [{"symbol": "AAPL", "ts": "2026-01-01T00:00:00+00:00", "polarity": 0.5}]
        bars = [self.Bar(f"2026-01-{d:02d}", 100 + d) for d in range(1, 11)]
        study = study_sentiment_signal(sentiment, bars, polarity_threshold=0.2, horizon_days=5)
        assert study is not None
        assert study.avg_forward_return > 0  # price rose after the positive read

    def test_low_polarity_ignored(self):
        sentiment = [{"symbol": "AAPL", "ts": "2026-01-01T00:00:00+00:00", "polarity": 0.05}]
        bars = [self.Bar(f"2026-01-{d:02d}", 100 + d) for d in range(1, 11)]
        assert study_sentiment_signal(sentiment, bars) is None

    def test_is_promising_gate(self):
        from trading.analytics.signal_research import SignalStudy
        good = SignalStudy("AAPL", samples=30, avg_forward_return=0.02, hit_rate=0.6)
        weak = SignalStudy("AAPL", samples=5, avg_forward_return=0.02, hit_rate=0.6)
        assert is_promising(good)
        assert not is_promising(weak)  # too few samples

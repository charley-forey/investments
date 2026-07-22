"""Continuous market-intelligence ingestion. Pulls news (Alpaca/Benzinga) and social
(Reddit via the sentiment provider), persists them deduped + ticker-linked into the
IntelStore, and records a per-symbol sentiment snapshot. Idempotent by content hash,
so it can run every few minutes without duplicating history."""

from __future__ import annotations

from dataclasses import dataclass

from ..config import Config
from .intel import IntelStore, NewsItem, SocialPost


@dataclass
class IngestReport:
    news_saved: int = 0
    social_saved: int = 0
    sentiment_snapshots: int = 0
    symbols: int = 0


def ingest_intel(config: Config, store: IntelStore, broker, provider=None) -> IngestReport:
    """Ingest news + sentiment for the configured universe. `provider` is a
    sentiment provider (defaults to the configured one); injectable for tests."""
    from .sentiment import get_provider

    provider = provider or get_provider(config)
    report = IngestReport()
    universe = list(config.settings.universe.core)
    try:
        from ..scanner.movers import active_candidate_symbols
        for s in active_candidate_symbols(config):
            if s not in universe:
                universe.append(s)
    except Exception:
        pass

    for symbol in universe:
        # News (always available via the broker).
        try:
            items = broker.get_news(symbol, limit=config.settings.agents.news_limit)
        except Exception:
            items = []
        news = [NewsItem(symbol=symbol, headline=i["headline"], summary=i.get("summary", ""),
                         source=i.get("source", ""), ts=i.get("created_at"))
                for i in items]
        report.news_saved += store.save_news(news)

        # Sentiment (news + reddit) via the provider, persisted as a snapshot + posts.
        try:
            sig = provider.score(config, broker, symbol)
        except Exception:
            continue
        store.record_sentiment(symbol=symbol, polarity=sig.polarity,
                               mention_count=sig.reddit_mentions,
                               source_mix=f"news+reddit({sig.reddit_mentions})")
        report.sentiment_snapshots += 1
        # Persist Reddit posts when the provider returned them (confirmation overlay).
        posts = getattr(sig, "reddit_posts", None) or []
        if posts:
            social = [
                SocialPost(
                    symbol=symbol,
                    text=p.get("title") or "",
                    platform="reddit",
                    author=p.get("author") or "",
                    score=int(p.get("score") or 0),
                )
                for p in posts if p.get("title")
            ]
            report.social_saved += store.save_social(social)

    report.symbols = len(universe)
    return report

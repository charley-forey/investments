"""Sentiment signal: Reddit (praw) + Alpaca news, distilled to a compact per-symbol
summary. This is an input to agent judgment, never a decision. Degrades
gracefully with no Reddit credentials (news-only)."""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..config import Config

# Tiny lexicon for a crude polarity lean. Not a sentiment model — just a signal
# the agent can weigh alongside the actual headlines.
_POSITIVE = {
    "beat", "beats", "surge", "surges", "soar", "rally", "upgrade", "bullish",
    "record", "growth", "strong", "gains", "gain", "outperform", "buy", "jumps",
    "breakout", "raise", "raised", "tops",
}
_NEGATIVE = {
    "miss", "misses", "plunge", "plunges", "crash", "downgrade", "bearish", "weak",
    "loss", "losses", "cuts", "cut", "lawsuit", "probe", "warning", "slump", "falls",
    "drops", "sell", "underperform", "halt", "recall",
}


@dataclass
class SentimentSignal:
    symbol: str
    reddit_mentions: int
    reddit_score: int
    polarity: float           # -1..1 crude lean from headlines + reddit titles
    top_headlines: list[str]

    def summary(self) -> str:
        lean = "positive" if self.polarity > 0.15 else "negative" if self.polarity < -0.15 else "neutral"
        lines = [
            f"{self.symbol}: polarity {self.polarity:+.2f} ({lean}); "
            f"reddit mentions={self.reddit_mentions} (score {self.reddit_score})"
        ]
        for h in self.top_headlines[:5]:
            lines.append(f"  - {h}")
        return "\n".join(lines)


def _polarity(texts: list[str]) -> float:
    pos = neg = 0
    for t in texts:
        for w in re.findall(r"[a-z']+", t.lower()):
            if w in _POSITIVE:
                pos += 1
            elif w in _NEGATIVE:
                neg += 1
    total = pos + neg
    return (pos - neg) / total if total else 0.0


def _reddit_client(config: Config):
    """Lazy praw client; None if credentials or praw are missing."""
    import os

    cid = os.getenv("REDDIT_CLIENT_ID")
    secret = os.getenv("REDDIT_CLIENT_SECRET")
    if not (cid and secret):
        return None
    try:
        import praw
    except ImportError:
        return None
    return praw.Reddit(
        client_id=cid, client_secret=secret,
        user_agent="agentic-trading-sentiment/0.1",
    )


def get_symbol_sentiment(
    config: Config, broker, symbol: str,
    subreddits: tuple[str, ...] = ("wallstreetbets", "stocks", "investing"),
    reddit=None,
) -> SentimentSignal:
    symbol = symbol.upper()
    texts: list[str] = []
    headlines: list[str] = []

    # News (always available via the broker).
    try:
        for item in broker.get_news(symbol, limit=config.settings.agents.news_limit):
            headlines.append(item["headline"])
            texts.append(item["headline"])
    except Exception:
        pass

    # Reddit (optional).
    mentions = 0
    score = 0
    client = reddit if reddit is not None else _reddit_client(config)
    if client is not None:
        try:
            for sub in subreddits:
                for post in client.subreddit(sub).search(symbol, limit=10, time_filter="week"):
                    title = getattr(post, "title", "")
                    if symbol in title.upper() or f"${symbol}" in title.upper():
                        mentions += 1
                        score += int(getattr(post, "score", 0) or 0)
                        texts.append(title)
        except Exception:
            pass

    return SentimentSignal(
        symbol=symbol,
        reddit_mentions=mentions,
        reddit_score=score,
        polarity=round(_polarity(texts), 3),
        top_headlines=headlines,
    )

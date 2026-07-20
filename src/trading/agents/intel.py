"""Market-intelligence curation agent: rolls the stored news/social stream into a
"what's moving the market and why" digest, persisted to the IntelStore and injected
into the strategy agent's context. This is the continuous-research layer."""

from __future__ import annotations

from ..config import Config
from ..data.intel import IntelStore
from . import prompts


def _recent_intel_text(store: IntelStore, universe: list[str]) -> str:
    lines = ["Recent news:"]
    for n in store.recent_news(limit=40):
        lines.append(f"- {n['symbol']} {n['headline']} ({n['source']})")
    lines.append("\nSentiment (latest per symbol):")
    for sym in universe:
        hist = store.sentiment_history(sym, days=3)
        if hist:
            last = hist[-1]
            lines.append(f"- {sym}: polarity {last['polarity']:+.2f}, "
                         f"mentions {last['mention_count']}")
    return "\n".join(lines)


def run_intel_session(client, config: Config, store: IntelStore) -> str:
    """Produce and persist a market-intel digest. Returns the digest markdown.
    A no-op-safe simple completion (no tools needed — the data is pre-assembled)."""
    universe = config.settings.universe.core
    context = _recent_intel_text(store, universe)
    system = [{"type": "text", "text": prompts.INTEL_SYSTEM,
               "cache_control": {"type": "ephemeral"}}]
    response = client.messages.create(
        model=config.settings.agents.model_for("scoring"),  # cheap-tier role
        max_tokens=config.settings.agents.max_tokens,
        thinking={"type": "adaptive"},
        system=system,
        messages=[{"role": "user", "content":
                   f"Here is the recent market intelligence:\n\n{context}\n\n"
                   "Write the digest."}],
    )
    digest = "\n".join(b.text for b in response.content if b.type == "text").strip()
    if digest:
        store.save_digest(digest, scope="market")
        try:
            from ..data.memory_vectors import remember_digest
            remember_digest(config, digest, scope="market")
        except Exception:
            pass
    return digest

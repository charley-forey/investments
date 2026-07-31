"""Market-intelligence curation agent: rolls the stored news/social stream into a
"what's moving the market and why" digest, persisted to the IntelStore and injected
into the strategy agent's context. This is the continuous-research layer.

When web_search is enabled for the intel role, the agent can pull live macro /
headline context beyond the stored Alpaca news stream.
"""

from __future__ import annotations

from ..config import Config
from ..cost import Usage, supports_adaptive_thinking, usage_from_response
from ..data.intel import IntelStore
from ..tools.assignment import web_search_tool_schema
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


def run_intel_session(client, config: Config, store: IntelStore,
                      usage: Usage | None = None) -> str:
    """Produce and persist a market-intel digest. Returns the digest markdown.

    Pass `usage` to have token/web-search spend accumulated into it; this loop is
    hand-rolled rather than run_agent's, and used to report nothing at all, so its
    cost never reached the ledger the daily cap reads."""
    universe = config.settings.universe.core
    context = _recent_intel_text(store, universe)
    resolved = config.settings.agents.tools_for("intel")
    system = [{"type": "text", "text": prompts.INTEL_SYSTEM,
               "cache_control": {"type": "ephemeral"}}]

    tools = []
    if resolved.web_search:
        tools.append(web_search_tool_schema(resolved.web_search_max_uses))

    user_content = (
        f"Here is the recent market intelligence:\n\n{context}\n\n"
        "Write the digest."
    )
    if resolved.web_search:
        user_content += (
            " Use web_search for macro/market-moving headlines that may not be "
            "in the stored feed (Fed, CPI, geopolitics, broad market themes)."
        )

    messages: list[dict] = [{"role": "user", "content": user_content}]
    model = config.settings.agents.model_for("scoring")  # cheap-tier role
    max_tokens = config.settings.agents.max_tokens
    max_iters = max(1, config.settings.agents.max_tool_iterations // 5)
    digest = ""

    # Same provider routing as the other two loops, so `scoring_model` may name a
    # gpt-* model. Adaptive thinking / effort translation lives in the adapter --
    # this loop no longer needs to know that Haiku 4.5 rejects adaptive thinking,
    # the 400 that killed the digest silently from 2026-07-23 to 07-29.
    from .providers import provider_for_model

    provider = provider_for_model(model, client)
    effort = config.settings.agents.effort_for("intel")

    for _ in range(max_iters):
        turn = provider.create(
            model=model, system=system, tools=tools, messages=messages,
            max_tokens=max_tokens, effort=effort,
            web_search=resolved.web_search,
            web_search_max_uses=resolved.web_search_max_uses)
        if usage is not None:
            usage.add(turn.usage)
            usage.web_searches += turn.web_searches
        if turn.text:
            digest = turn.text.strip()

        # web_search is server-side on both providers: the search already ran, so
        # there are no client tool results to send back. Echo the turn and nudge.
        if turn.stop_reason == "pause_turn":
            provider.append_assistant(messages, turn)
            continue
        if turn.stop_reason == "tool_use":
            provider.append_assistant(messages, turn)
            messages.append({
                "role": "user",
                "content": "Continue and produce the final digest now.",
            })
            continue
        break

    if digest:
        store.save_digest(digest, scope="market")
        try:
            from ..data.memory_vectors import remember_digest
            remember_digest(config, digest, scope="market")
        except Exception:
            pass
    return digest

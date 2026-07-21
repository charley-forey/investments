"""Market-intelligence curation agent: rolls the stored news/social stream into a
"what's moving the market and why" digest, persisted to the IntelStore and injected
into the strategy agent's context. This is the continuous-research layer.

When web_search is enabled for the intel role, the agent can pull live macro /
headline context beyond the stored Alpaca news stream.
"""

from __future__ import annotations

from ..config import Config
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


def run_intel_session(client, config: Config, store: IntelStore) -> str:
    """Produce and persist a market-intel digest. Returns the digest markdown."""
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

    for _ in range(max_iters):
        kwargs: dict = dict(
            model=model,
            max_tokens=max_tokens,
            thinking={"type": "adaptive"},
            system=system,
            messages=messages,
        )
        if tools:
            kwargs["tools"] = tools
        response = client.messages.create(**kwargs)

        text_parts = [b.text for b in response.content if getattr(b, "type", None) == "text"]
        if text_parts:
            digest = "\n".join(text_parts).strip()

        # Server-side web_search: Anthropic executes it; we just continue the loop
        # if the model still wants client tools (intel has none) or pause_turn.
        if response.stop_reason == "pause_turn":
            messages.append({"role": "assistant", "content": response.content})
            continue
        if response.stop_reason == "tool_use":
            # Only server tools expected; append assistant content and continue
            # so the model can finish after search results already in the turn.
            messages.append({"role": "assistant", "content": response.content})
            # No client tool_results to send; if only server tools ran, Anthropic
            # usually ends with end_turn on a subsequent call once we echo.
            # Nudge completion:
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

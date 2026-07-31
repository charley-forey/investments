"""Generic manual agentic loop.

We run the loop by hand (not the SDK tool runner) so every tool call is
journaled and `propose_order` can only register drafts. The system prompt is
frozen text with a cache_control breakpoint; all volatile data (account state,
quotes) reaches the model through tool results.

Anthropic server-side tools (web_search) are appended to the tools list when
enabled; they execute on Anthropic's infrastructure and appear as
server_tool_use / web_search_tool_result blocks — we journal them but do not
dispatch them locally.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..cost import Usage, supports_adaptive_thinking, usage_from_response
from ..data.journal import Journal
from ..guardrails.models import OrderProposal
from ..resilience import RetryConfig, with_retry
from ..tools.assignment import WEB_SEARCH, web_search_tool_schema
from ..tools.registry import ToolRegistry


@dataclass
class AgentResult:
    final_text: str
    drafts: list[OrderProposal]
    iterations: int
    stop_reason: str
    usage: Usage = None       # accumulated token usage across the loop
    reasoning: str = ""       # accumulated summarized thinking (transparency)
    tool_calls: list = None   # (name, input) tool calls made, in order

    def __post_init__(self):
        if self.usage is None:
            self.usage = Usage()
        if self.tool_calls is None:
            self.tool_calls = []


def _roll_cache_breakpoint(messages: list[dict]) -> None:
    """Keep one cache breakpoint on the newest tool-result block.

    Only the system prompt was cached, so every tool result was re-billed at full
    rate on each of up to 25 iterations — a ~30% cache hit rate on calls running
    20k-160k input tokens. Moving a single breakpoint to the tail caches the whole
    accumulated history as a prefix. Assistant turns hold SDK objects rather than
    dicts, so the marker rides on the tool-result blocks we build ourselves.
    """
    for msg in messages:
        content = msg.get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict):
                    block.pop("cache_control", None)
    tail = messages[-1].get("content") if messages else None
    if isinstance(tail, list) and tail and isinstance(tail[-1], dict):
        tail[-1]["cache_control"] = {"type": "ephemeral"}


def run_agent(
    client,
    *,
    model: str,
    max_tokens: int,
    system_prompt: str,
    registry: ToolRegistry,
    user_message: str,
    max_iterations: int,
    journal: Journal | None = None,
    agent_name: str = "agent",
    web_search: bool = False,
    web_search_max_uses: int = 0,
    effort: str | None = None,
) -> AgentResult:
    # 1h TTL, not the 5m default. Render order is tools -> system -> messages, so
    # this one breakpoint covers the frozen system prompt AND the tool schemas.
    # Intraday sessions land minutes apart, so at 5m every session re-wrote a
    # prefix that never changes: 635k write tokens on 2026-07-29 = $3.97 of a
    # $15.07 day. 1h costs 2x on write vs 1.25x and breaks even at 3 reads; we get
    # ~25/hour. The rolling tool-result breakpoint below stays at 5m -- that
    # content IS per-session, and iterations are seconds apart.
    system = [{"type": "text", "text": system_prompt,
               "cache_control": {"type": "ephemeral", "ttl": "1h"}}]
    tools = list(registry.schemas())
    if web_search and web_search_max_uses > 0:
        tools.append(web_search_tool_schema(web_search_max_uses))
    messages: list[dict] = [{"role": "user", "content": user_message}]

    stop_reason = "max_iterations"
    iterations = 0
    final_text = ""
    total_usage = Usage()
    reasoning_parts: list[str] = []
    tool_calls: list = []

    # Which vendor answers is decided by the model id alone, so `model_by_cycle` in
    # settings.yaml is the whole switch. The loop below is identical either way --
    # it was already provider-agnostic in shape; only the encoding differed.
    from .providers import provider_for_model

    provider = provider_for_model(model, client)

    while iterations < max_iterations:
        iterations += 1

        turn = with_retry(
            lambda: provider.create(
                model=model, system=system, tools=tools, messages=messages,
                max_tokens=max_tokens, effort=effort,
                web_search=web_search, web_search_max_uses=web_search_max_uses),
            config=RetryConfig(retries=2))

        total_usage.add(turn.usage)
        total_usage.web_searches += turn.web_searches
        if turn.reasoning:
            reasoning_parts.append(turn.reasoning)
        if turn.text:
            final_text = turn.text

        if turn.stop_reason == "pause_turn":
            provider.append_assistant(messages, turn)
            continue

        if turn.stop_reason != "tool_use":
            stop_reason = turn.stop_reason or "end_turn"
            break

        provider.append_assistant(messages, turn)
        tool_results = []
        called = []
        for call in turn.tool_calls:
            result = registry.dispatch(call.name, call.input)
            called.append(call.name)
            tool_calls.append({"name": call.name, "input": call.input})
            tool_results.append({
                "call_id": call.call_id,
                "content": result,
                "is_error": result.startswith("error:"),
            })
        if tool_results:
            provider.append_tool_results(messages, tool_results)
            provider.roll_cache_breakpoint(messages)
        if journal is not None and called:
            journal.heartbeat(f"agent:{agent_name}", detail=f"tools: {', '.join(called)}")

    return AgentResult(
        final_text=final_text,
        drafts=list(registry.ctx.drafts),
        iterations=iterations,
        stop_reason=stop_reason,
        usage=total_usage,
        reasoning="\n\n".join(reasoning_parts),
        tool_calls=tool_calls,
    )

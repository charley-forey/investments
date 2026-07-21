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

from ..cost import Usage, usage_from_response
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
) -> AgentResult:
    system = [{"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}]
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

    while iterations < max_iterations:
        iterations += 1

        def _create():
            kwargs = dict(
                model=model,
                max_tokens=max_tokens,
                # summarized thinking so the agent's reasoning can be captured for
                # transparency (the raw chain of thought is never exposed).
                thinking={"type": "adaptive", "display": "summarized"},
                system=system,
                messages=messages,
            )
            if tools:
                kwargs["tools"] = tools
            return client.messages.create(**kwargs)

        response = with_retry(_create, config=RetryConfig(retries=2))
        total_usage.add(usage_from_response(response))
        for block in response.content:
            if getattr(block, "type", None) == "thinking":
                t = getattr(block, "thinking", "")
                if t:
                    reasoning_parts.append(t)

        if response.stop_reason == "pause_turn":
            messages.append({"role": "assistant", "content": response.content})
            continue

        text_parts = [b.text for b in response.content if b.type == "text"]
        if text_parts:
            final_text = "\n".join(text_parts)

        if response.stop_reason != "tool_use":
            # Still journal any server-side searches that completed this turn.
            for block in response.content:
                if (getattr(block, "type", None) == "server_tool_use"
                        and getattr(block, "name", "") == WEB_SEARCH):
                    tool_calls.append({
                        "name": WEB_SEARCH,
                        "input": getattr(block, "input", None) or {},
                    })
            stop_reason = response.stop_reason or "end_turn"
            break

        messages.append({"role": "assistant", "content": response.content})
        tool_results = []
        called = []
        for block in response.content:
            btype = getattr(block, "type", None)
            if btype == "server_tool_use" and getattr(block, "name", "") == WEB_SEARCH:
                called.append(WEB_SEARCH)
                tool_calls.append({
                    "name": WEB_SEARCH,
                    "input": getattr(block, "input", None) or {},
                })
                continue
            if btype != "tool_use":
                continue
            result = registry.dispatch(block.name, block.input or {})
            called.append(block.name)
            tool_calls.append({"name": block.name, "input": block.input or {}})
            tool_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result,
                    "is_error": result.startswith("error:"),
                }
            )
        if tool_results:
            messages.append({"role": "user", "content": tool_results})
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

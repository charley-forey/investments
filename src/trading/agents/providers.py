"""One agent loop, two wire formats.

The loop in `runner.py` is genuinely provider-agnostic: send a system prompt, some
tools and a transcript; get back text, reasoning and tool calls; dispatch the
tools; repeat. Only the encoding differs. This module is that encoding, so the
loop, the guardrails and the journal never learn which vendor answered.

Routing is by model id (`gpt-*` -> OpenAI), so `model_by_cycle` in settings.yaml
is the only switch. Nothing else in the system needs a flag.

Two differences worth knowing, because they change what the cost ledger means:

* **Caching.** Anthropic needs explicit `cache_control` breakpoints and charges a
  premium to WRITE the cache (1.25x, or 2.0x at 1h TTL). OpenAI caches
  automatically and charges nothing to populate. On 2026-07-30's real volumes the
  Anthropic cache cost $7.61 of a $13.74 day -- so this, not the headline token
  rate, is most of the provider gap.
* **Reasoning.** Anthropic returns summarized thinking blocks. OpenAI returns
  reasoning items whose content is not exposed, but whose token count is, and
  those tokens are billed as output. `Usage.reasoning_tokens` records them so a
  verbose-but-cheap model can be told apart from a terse-but-expensive one --
  which is exactly the comparison that decided this.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..cost import Usage, provider_for, supports_adaptive_thinking


@dataclass
class ToolCall:
    """A tool the model wants run. `call_id` is whatever the provider needs back."""
    name: str
    input: dict
    call_id: str


@dataclass
class Turn:
    """One model response, normalised."""
    text: str = ""
    reasoning: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    stop_reason: str = "end_turn"
    raw: object = None          # provider-native content, for replay
    web_searches: int = 0


class Provider:
    """Encode a request, decode a response, and append turns to a transcript."""

    def create(self, *, model, system, tools, messages, max_tokens, effort,
               web_search=False, web_search_max_uses=0):
        raise NotImplementedError

    def append_assistant(self, messages: list, turn: Turn) -> None:
        raise NotImplementedError

    def append_tool_results(self, messages: list, results: list[dict]) -> None:
        raise NotImplementedError

    def roll_cache_breakpoint(self, messages: list) -> None:
        """No-op unless the provider needs manual cache placement."""

    def create_json(self, *, model, system, messages, schema, max_tokens):
        """A final turn constrained to `schema`. Returns (json_text, Usage)."""
        raise NotImplementedError


# --------------------------------------------------------------------------- #
# Anthropic
# --------------------------------------------------------------------------- #

class AnthropicProvider(Provider):
    def __init__(self, client):
        self.client = client

    def create(self, *, model, system, tools, messages, max_tokens, effort,
               web_search=False, web_search_max_uses=0):
        from ..cost import usage_from_response

        kwargs = dict(model=model, max_tokens=max_tokens, system=system,
                      messages=messages)
        if supports_adaptive_thinking(model):
            kwargs["thinking"] = {"type": "adaptive", "display": "summarized"}
        if effort:
            kwargs["output_config"] = {"effort": effort}
        if tools:
            kwargs["tools"] = tools
        resp = self.client.messages.create(**kwargs)

        from ..tools.assignment import WEB_SEARCH

        turn = Turn(usage=usage_from_response(resp), raw=resp.content,
                    stop_reason=resp.stop_reason or "end_turn")
        texts, thinking = [], []
        for b in resp.content:
            bt = getattr(b, "type", None)
            if bt == "text":
                texts.append(b.text)
            elif bt == "thinking":
                if getattr(b, "thinking", ""):
                    thinking.append(b.thinking)
            elif bt == "server_tool_use" and getattr(b, "name", "") == WEB_SEARCH:
                turn.web_searches += 1
            elif bt == "tool_use":
                turn.tool_calls.append(
                    ToolCall(name=b.name, input=b.input or {}, call_id=b.id))
        turn.text = "\n".join(texts)
        turn.reasoning = "\n".join(thinking)
        return turn

    def append_assistant(self, messages, turn):
        messages.append({"role": "assistant", "content": turn.raw})

    def append_tool_results(self, messages, results):
        messages.append({
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": r["call_id"],
                 "content": r["content"], "is_error": r["is_error"]}
                for r in results
            ],
        })

    def roll_cache_breakpoint(self, messages):
        from .runner import _roll_cache_breakpoint
        _roll_cache_breakpoint(messages)

    def create_json(self, *, model, system, messages, schema, max_tokens):
        from ..cost import usage_from_response

        resp = self.client.messages.create(
            model=model, max_tokens=max_tokens, system=system, messages=messages,
            output_config={"format": {"type": "json_schema", "schema": schema}},
        )
        text = next((b.text for b in resp.content if b.type == "text"), "{}")
        return text, usage_from_response(resp)


# --------------------------------------------------------------------------- #
# OpenAI
# --------------------------------------------------------------------------- #

def _instructions(system) -> str:
    """Anthropic takes `system` as a list of cache-annotated blocks; OpenAI takes
    one instruction string and handles caching itself."""
    if isinstance(system, list):
        return "\n\n".join(
            b["text"] for b in system if isinstance(b, dict) and b.get("text"))
    return str(system)


def _openai_tools(tools: list[dict], *, web_search: bool = False) -> list[dict]:
    """Anthropic tool schemas -> OpenAI tools.

    `input_schema` becomes `parameters`; everything else is the same JSON Schema.
    Anthropic's server-side web_search block is replaced with OpenAI's own
    server-side `web_search` rather than dropped -- the intel agent has NO other
    tools, so losing it would have quietly reduced the market digest to a summary
    of already-stored text. That capability is not optional: it is the only reason
    the digest knows anything the journal does not.
    """
    out = []
    for t in tools:
        if "input_schema" not in t:
            continue  # server-side tool; handled below
        out.append({
            "type": "function",
            "name": t["name"],
            "description": t.get("description", ""),
            "parameters": t["input_schema"],
        })
    if web_search:
        out.append({"type": "web_search"})
    return out


class OpenAIProvider(Provider):
    """Responses API. Chat Completions cannot carry reasoning items between turns,
    which an agent loop needs."""

    def __init__(self, client):
        self.client = client

    def create(self, *, model, system, tools, messages, max_tokens, effort,
               web_search=False, web_search_max_uses=0):
        import json

        kwargs = dict(model=model, instructions=_instructions(system), input=messages,
                      max_output_tokens=max_tokens)
        if effort:
            kwargs["reasoning"] = {"effort": effort}
        ot = _openai_tools(tools or [],
                           web_search=bool(web_search and web_search_max_uses > 0))
        if ot:
            kwargs["tools"] = ot
        resp = self.client.responses.create(**kwargs)

        u = resp.usage
        det = getattr(u, "output_tokens_details", None)
        cached = getattr(getattr(u, "input_tokens_details", None),
                         "cached_tokens", 0) or 0
        total_in = int(getattr(u, "input_tokens", 0) or 0)
        usage = Usage(
            # OpenAI reports input_tokens INCLUSIVE of cached; the ledger treats
            # the three counts as disjoint, so subtract.
            input_tokens=max(0, total_in - int(cached)),
            output_tokens=int(getattr(u, "output_tokens", 0) or 0),
            cache_read_tokens=int(cached),
            reasoning_tokens=int(getattr(det, "reasoning_tokens", 0) or 0) if det else 0,
        )

        turn = Turn(usage=usage, raw=resp.output, text=(resp.output_text or "").strip())
        for item in resp.output or []:
            itype = getattr(item, "type", None)
            if itype == "web_search_call":
                # Server-side and billed per call, like Anthropic's. Counted so it
                # reaches the daily cap rather than running off-book.
                turn.web_searches += 1
                continue
            if itype == "function_call":
                try:
                    args = json.loads(item.arguments or "{}")
                except (ValueError, TypeError):
                    args = {}
                turn.tool_calls.append(
                    ToolCall(name=item.name, input=args, call_id=item.call_id))
        turn.stop_reason = "tool_use" if turn.tool_calls else "end_turn"
        return turn

    def append_assistant(self, messages, turn):
        # Reasoning items must be echoed back or the next turn loses the chain.
        messages.extend(turn.raw or [])

    def append_tool_results(self, messages, results):
        for r in results:
            messages.append({
                "type": "function_call_output",
                "call_id": r["call_id"],
                "output": r["content"],
            })

    def create_json(self, *, model, system, messages, schema, max_tokens):
        instructions = _instructions(system)
        # Responses API needs a NAMED schema and rejects a bare one. It also
        # requires every property to be listed in `required` under strict mode,
        # which the verdict schema does not guarantee -- so strict is off and the
        # caller keeps its existing json.loads + validation path.
        resp = self.client.responses.create(
            model=model, instructions=instructions, input=messages,
            max_output_tokens=max_tokens,
            text={"format": {"type": "json_schema", "name": "verdict",
                             "schema": schema, "strict": False}},
        )
        u = resp.usage
        det = getattr(u, "output_tokens_details", None)
        cached = getattr(getattr(u, "input_tokens_details", None), "cached_tokens", 0) or 0
        usage = Usage(
            input_tokens=max(0, int(getattr(u, "input_tokens", 0) or 0) - int(cached)),
            output_tokens=int(getattr(u, "output_tokens", 0) or 0),
            cache_read_tokens=int(cached),
            reasoning_tokens=int(getattr(det, "reasoning_tokens", 0) or 0) if det else 0,
        )
        return (resp.output_text or "{}").strip(), usage


def provider_for_model(model: str, anthropic_client, openai_client=None) -> Provider:
    """Pick the adapter from the model id. `model_by_cycle` is the whole switch."""
    if provider_for(model) == "openai":
        if openai_client is None:
            from .client import openai_client as _mk
            openai_client = _mk()
        return OpenAIProvider(openai_client)
    return AnthropicProvider(anthropic_client)

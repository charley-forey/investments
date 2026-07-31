"""One agent loop, two wire formats.

The loop was already provider-agnostic in shape -- send system + tools +
transcript, get back text, reasoning and tool calls, dispatch, repeat. Only the
encoding differed, so the port is an adapter rather than a rewrite. Routing is by
model id, which makes `model_by_cycle` in settings.yaml the entire switch.

Measured on identical live sessions 2026-07-31:

    agent loop   terra $0.0186  vs  opus-4.8 $0.0924   (5.0x)
    risk review  terra $0.0397  vs  opus-4.8 $0.1537   (3.9x)

and on 2026-07-30's real token volumes, a full day would have been $0.54 on luna
and $5.44 on terra against $13.74 on opus-4.8. Most of that gap is NOT the
headline token rate: Anthropic's cache write premium was $7.61 of that day, and
OpenAI populates its cache for free.
"""

import json

import pytest

from trading.agents.providers import (
    AnthropicProvider, OpenAIProvider, Turn, _instructions, _openai_tools,
    provider_for_model,
)
from trading.cost import PRICING, Usage, estimate_cost, provider_for


# -- routing ------------------------------------------------------------------

def test_model_id_is_the_only_switch():
    assert provider_for("gpt-5.6-terra") == "openai"
    assert provider_for("gpt-5.6-luna") == "openai"
    assert provider_for("claude-opus-4-8") == "anthropic"
    assert provider_for("") == "anthropic"


def test_provider_for_model_picks_the_adapter():
    assert isinstance(provider_for_model("claude-opus-4-8", object()), AnthropicProvider)
    assert isinstance(
        provider_for_model("gpt-5.6-terra", object(), openai_client=object()),
        OpenAIProvider)


# -- schema translation -------------------------------------------------------

def test_tool_schemas_translate():
    anthro = [{"name": "get_quote", "description": "quote",
               "input_schema": {"type": "object",
                                "properties": {"symbol": {"type": "string"}}}}]
    out = _openai_tools(anthro)
    assert out == [{"type": "function", "name": "get_quote", "description": "quote",
                    "parameters": {"type": "object",
                                   "properties": {"symbol": {"type": "string"}}}}]


def test_server_side_tools_are_dropped_not_faked():
    """web_search has no OpenAI counterpart. A silently missing capability is
    worse than an absent one, so it is dropped here and warned about at call
    time rather than translated into something that does not search."""
    assert _openai_tools([{"type": "web_search_20260209", "name": "web_search"}]) == []


def test_system_blocks_collapse_to_instructions():
    system = [{"type": "text", "text": "A", "cache_control": {"type": "ephemeral"}},
              {"type": "text", "text": "B"}]
    assert _instructions(system) == "A\n\nB"
    assert _instructions("plain") == "plain"


# -- pricing ------------------------------------------------------------------

def test_openai_models_are_priced_so_the_cap_is_real():
    """Without these entries an OpenAI call falls through to _DEFAULT (Opus
    rates) and the daily cap is enforced against invented numbers -- the same
    class of failure as the web_search spend that ran entirely off-book."""
    for m in ("gpt-5.6-luna", "gpt-5.6-terra", "gpt-5.6-sol"):
        assert m in PRICING


def test_openai_cache_writes_are_free_not_a_premium():
    """The structural difference. Anthropic charges 1.25x to populate the cache;
    OpenAI charges nothing. That was $7.61 of a $13.74 day."""
    u = Usage(cache_write_tokens=1_000_000)
    openai_cost = estimate_cost(u, "gpt-5.6-terra")
    plain_input = estimate_cost(Usage(input_tokens=1_000_000), "gpt-5.6-terra")
    assert openai_cost == pytest.approx(plain_input)

    anthropic_cost = estimate_cost(u, "claude-opus-4-8")
    anthropic_plain = estimate_cost(Usage(input_tokens=1_000_000), "claude-opus-4-8")
    assert anthropic_cost > anthropic_plain


def test_cached_input_uses_the_published_rate():
    u = Usage(cache_read_tokens=1_000_000)
    assert estimate_cost(u, "gpt-5.6-luna") == pytest.approx(0.02)
    assert estimate_cost(u, "gpt-5.6-terra") == pytest.approx(0.20)


def test_the_verbose_cheap_model_still_wins_on_price():
    """luna spent 602 output tokens on a probe where terra spent 187 -- 3.2x. At
    $1.20 vs $12.00 per MTok it is still ~3x cheaper, which inverts the usual
    'cheaper per token is not cheaper per decision' warning. Worth pinning,
    because the opposite has been true twice in this codebase."""
    luna = estimate_cost(Usage(input_tokens=95, output_tokens=602), "gpt-5.6-luna")
    terra = estimate_cost(Usage(input_tokens=95, output_tokens=187), "gpt-5.6-terra")
    assert luna < terra


# -- decoding -----------------------------------------------------------------

class _FakeResponses:
    def __init__(self, output, usage):
        self._output, self._usage = output, usage

    def create(self, **kw):
        self.kw = kw
        return type("R", (), {
            "output": self._output, "usage": self._usage,
            "output_text": "".join(getattr(o, "text", "") for o in self._output),
        })()


class _Usage:
    def __init__(self, inp, out, cached=0, reasoning=0):
        self.input_tokens, self.output_tokens = inp, out
        self.input_tokens_details = type("D", (), {"cached_tokens": cached})()
        self.output_tokens_details = type("D", (), {"reasoning_tokens": reasoning})()


def test_openai_input_tokens_are_made_disjoint():
    """OpenAI reports input_tokens INCLUSIVE of cached; the ledger treats the
    three counts as disjoint. Double-counting here would inflate every cost."""
    call = type("C", (), {"type": "function_call", "name": "get_quote",
                          "arguments": '{"symbol":"MSFT"}', "call_id": "c1"})()
    client = type("Cl", (), {})()
    client.responses = _FakeResponses([call], _Usage(inp=1000, out=50, cached=800,
                                                     reasoning=20))
    turn = OpenAIProvider(client).create(
        model="gpt-5.6-terra", system=[{"type": "text", "text": "s"}], tools=[],
        messages=[], max_tokens=100, effort=None)
    assert turn.usage.input_tokens == 200
    assert turn.usage.cache_read_tokens == 800
    assert turn.usage.reasoning_tokens == 20


def test_openai_function_calls_become_tool_calls():
    call = type("C", (), {"type": "function_call", "name": "get_quote",
                          "arguments": '{"symbol":"MSFT"}', "call_id": "c1"})()
    client = type("Cl", (), {})()
    client.responses = _FakeResponses([call], _Usage(10, 10))
    turn = OpenAIProvider(client).create(
        model="gpt-5.6-terra", system="s", tools=[], messages=[],
        max_tokens=100, effort=None)
    assert turn.stop_reason == "tool_use"
    assert turn.tool_calls[0].name == "get_quote"
    assert turn.tool_calls[0].input == {"symbol": "MSFT"}
    assert turn.tool_calls[0].call_id == "c1"


def test_malformed_tool_arguments_do_not_crash_the_loop():
    call = type("C", (), {"type": "function_call", "name": "get_quote",
                          "arguments": "{not json", "call_id": "c1"})()
    client = type("Cl", (), {})()
    client.responses = _FakeResponses([call], _Usage(10, 10))
    turn = OpenAIProvider(client).create(
        model="gpt-5.6-terra", system="s", tools=[], messages=[],
        max_tokens=100, effort=None)
    assert turn.tool_calls[0].input == {}


def test_tool_results_use_each_providers_shape():
    results = [{"call_id": "c1", "content": "ok", "is_error": False}]

    a_msgs = []
    AnthropicProvider(object()).append_tool_results(a_msgs, results)
    assert a_msgs[0]["role"] == "user"
    assert a_msgs[0]["content"][0]["tool_use_id"] == "c1"

    o_msgs = []
    OpenAIProvider(object()).append_tool_results(o_msgs, results)
    assert o_msgs[0] == {"type": "function_call_output", "call_id": "c1",
                         "output": "ok"}


def test_openai_replays_reasoning_items_verbatim():
    """Reasoning items must be echoed back or the next turn loses the chain."""
    raw = [{"type": "reasoning", "id": "r1"}, {"type": "message"}]
    msgs = []
    OpenAIProvider(object()).append_assistant(msgs, Turn(raw=raw))
    assert msgs == raw

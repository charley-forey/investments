"""Regression for the market digest that died silently on 2026-07-23.

Commit 1927c7f pointed the cheap-tier role at claude-haiku-4-5. The intel loop
sends thinking={"type": "adaptive"}, which Haiku 4.5 rejects with a 400 -- it
needs {"type": "enabled", "budget_tokens": N}. Every premarket cycle from 07-23
to 07-29 therefore 400'd on its first call.

Nothing noticed for six days because the exception was caught into
CycleReport.notes and summary() never rendered notes, and because the dashboard
discarded the digest timestamp -- so a frozen digest looked exactly like a fresh
one. Three separate failures of observability stacked on one wrong kwarg.

The fix belongs where model and thinking config meet, not in intel.py: runner.py
sends the same kwarg to whatever model a role resolves to, so ANY role landing on
an older model would fail identically.
"""

import pytest

from trading.cost import supports_adaptive_thinking


class RecordingClient:
    """Captures create() kwargs and returns a minimal well-formed response."""

    def __init__(self):
        self.calls = []
        self.messages = self

    def create(self, **kwargs):
        self.calls.append(kwargs)

        class _Block:
            type = "text"
            text = "# Digest\nAll quiet."

        class _Resp:
            content = [_Block()]
            stop_reason = "end_turn"
            usage = None

        return _Resp()


def test_haiku_45_does_not_get_adaptive_thinking():
    assert not supports_adaptive_thinking("claude-haiku-4-5")


def test_current_models_do_get_adaptive_thinking():
    for model in ("claude-opus-4-8", "claude-opus-5", "claude-sonnet-5",
                  "claude-sonnet-4-6", "claude-fable-5"):
        assert supports_adaptive_thinking(model), model


def test_an_unknown_model_falls_back_to_no_thinking():
    """Unknown must mean 'omit', never 'guess adaptive'. A missing thinking block
    degrades an answer; a 400 loses it entirely, which is what happened here."""
    assert not supports_adaptive_thinking("some-future-model")


def test_intel_session_omits_thinking_on_the_cheap_tier(config, tmp_path):
    """End to end on the exact path that broke: the digest agent resolves through
    scoring_model, and must not send a kwarg that model rejects."""
    from trading.agents.intel import run_intel_session
    from trading.data.intel import IntelStore

    config.settings.agents.scoring_model = "claude-haiku-4-5"
    client = RecordingClient()
    store = IntelStore(tmp_path / "intel.db")

    digest = run_intel_session(client, config, store)

    assert client.calls, "the digest agent never called the API"
    assert client.calls[0]["model"] == "claude-haiku-4-5"
    assert "thinking" not in client.calls[0], \
        "adaptive thinking on Haiku 4.5 is a 400 — this is the six-day outage"
    assert digest, "a digest should still be produced"


def test_intel_session_keeps_thinking_on_a_capable_model(config, tmp_path):
    from trading.agents.intel import run_intel_session
    from trading.data.intel import IntelStore

    config.settings.agents.scoring_model = "claude-opus-4-8"
    client = RecordingClient()
    run_intel_session(client, config, IntelStore(tmp_path / "intel.db"))

    # `display: summarized` since intel moved onto the shared provider adapter --
    # same thinking config as every other Anthropic agent. Costs nothing extra;
    # thinking is billed identically whether or not a summary is returned.
    assert client.calls[0]["thinking"] == {"type": "adaptive",
                                           "display": "summarized"}

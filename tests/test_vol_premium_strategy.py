"""The one strategy in the registry that is not a trend bet.

Every other tag is a long-vol directional rule. They want the same tape, which is
why they share a regime signature and fail in the same cells -- so adding another
one is not diversification. This is short-vol and mean-reverting: it sells rich
premium and profits from price NOT moving.

Two defects kept it from ever firing:
  1. It returned None in sideways tape, because it demanded a trend for direction.
     Sideways is the regime the system is actually in, so a module built to
     diversify away from trend-following could only fire when the trend-followers
     were already firing.
  2. It was reachable only through get_options_chain -- a tool the agent called
     ZERO times in 1,049 tool calls on 2026-07-29. Not unwired, undiscoverable.
"""

import json

import pytest

from trading import strategies as registry
from trading.scanner.vol_premium import (
    HIGH_IV_RANK, LOW_IV_RANK, MIN_STRETCH, scan_context, suggest_vol_structure,
)


# -- registry -----------------------------------------------------------------

def test_vol_premium_is_proposable_and_has_a_playbook():
    s = registry.get("vol-premium")
    assert s is not None and s.proposable
    assert s.playbook == "vol-premium"


def test_vol_premium_is_not_backtestable_and_says_so():
    """No options price history exists, so it can never pass the sweep gate. It
    must stay at `unproven` (0.25x) and earn its record live -- not be quietly
    exempted from validation."""
    s = registry.get("vol-premium")
    assert not s.backtestable
    assert s not in registry.backtestable()


# -- structure selection ------------------------------------------------------

def test_rich_iv_with_no_event_sells_premium():
    assert suggest_vol_structure(HIGH_IV_RANK + 5, "up", False) == ("credit", "bullish")


def test_rich_iv_into_an_event_is_refused():
    """IV is rich before earnings because the move is genuinely uncertain.
    Collecting that premium is picking up pennies in front of what prices it."""
    assert suggest_vol_structure(95.0, "up", True) is None


def test_cheap_iv_with_a_catalyst_buys_premium():
    assert suggest_vol_structure(LOW_IV_RANK - 5, "down", True) == ("debit", "bearish")


def test_mid_range_iv_is_no_trade():
    assert suggest_vol_structure(50.0, "up", False) is None


def test_sideways_tape_leans_against_the_recent_move():
    """The fix that matters. A name that ran up inside a rangebound tape is the one
    to sell calls against; one that dumped is the one to sell puts against."""
    up = suggest_vol_structure(90.0, "sideways", False, stretch=+0.10)
    down = suggest_vol_structure(90.0, "sideways", False, stretch=-0.10)
    assert up == ("credit", "bearish")
    assert down == ("credit", "bullish")


def test_sideways_with_no_meaningful_move_has_no_side():
    assert suggest_vol_structure(90.0, "sideways", False,
                                 stretch=MIN_STRETCH / 2) is None


def test_sideways_without_stretch_data_is_still_none():
    assert suggest_vol_structure(90.0, "sideways", False, stretch=None) is None


def test_unknown_regime_gives_no_lean():
    assert suggest_vol_structure(90.0, None, False, stretch=0.10) is None


# -- discoverability ----------------------------------------------------------

def _snap(journal, symbol, iv_rank, momentum):
    return journal.record_snapshot(
        cycle="intraday", symbol=symbol, bid=None, ask=None, last=100.0,
        spread_bps=None, features={"momentum_20": momentum}, sentiment=None,
        mention_count=None, template=None, trigger_direction=None,
        regime_trend="sideways", regime_vol="calm", iv_rank=iv_rank)


def test_scan_surfaces_rich_iv_names_without_a_chain_call(journal):
    _snap(journal, "RICH", 92.0, 0.10)
    ctx = scan_context(journal, "sideways")
    assert "RICH" in ctx and "credit" in ctx and "bearish" in ctx


def test_scan_skips_names_with_no_vol_trade(journal):
    _snap(journal, "MID", 50.0, 0.10)
    assert scan_context(journal, "sideways") == ""


def test_scan_warns_about_selling_into_an_event(journal):
    """The scan does not do a calendar lookup, so the text must carry the veto the
    agent has to apply itself."""
    _snap(journal, "RICH", 92.0, 0.10)
    assert "binary event" in scan_context(journal, "sideways")

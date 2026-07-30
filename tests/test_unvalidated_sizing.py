"""A strategy with no evidence anywhere does not get full size.

`regime_size_multiplier` returns 1.0 for an unknown (tag, regime) because absence
of evidence must not shrink a position that other evidence supports. Applied to a
tag with no record ANYWHERE that rule inverts: the strategy runs at full size in
every regime precisely because nothing has ever measured it.

vol-premium was exactly that. It cannot be backtested -- there is no options price
history -- so it was the only tag tradeable in all eight regimes, the only one
available in up/calm, at full size, on no evidence at all.

The `unproven` lifecycle stage does NOT catch this. `live_size_scale` in
guardrails/engine.py is computed only under `if self.config.is_live`, so in paper
mode the stage gates at zero and never scales anything.
"""

import pytest

from trading.analytics.sweep import UNVALIDATED_MULT, unvalidated_multiplier
from trading.data.journal import Journal


def test_a_tag_with_no_record_is_discounted(tmp_path):
    j = Journal(tmp_path / "j.db")
    assert unvalidated_multiplier(j, "vol-premium") == UNVALIDATED_MULT


def test_a_tag_with_a_record_is_not_discounted(tmp_path):
    j = Journal(tmp_path / "j.db")
    j.set_state("backtest:breakout", '{"mean_alpha_r": -1.9, "regime": {}}')
    assert unvalidated_multiplier(j, "breakout") == 1.0


def test_the_discount_lifts_once_evidence_exists(tmp_path):
    """'Until graded' -- a strategy awaiting its first sweep is discounted, and the
    discount lifts by itself the night the sweep records a result."""
    j = Journal(tmp_path / "j.db")
    assert unvalidated_multiplier(j, "mean-reversion") == UNVALIDATED_MULT
    j.set_state("backtest:mean-reversion", '{"mean_alpha_r": -1.2, "regime": {}}')
    assert unvalidated_multiplier(j, "mean-reversion") == 1.0


def test_a_failing_record_still_counts_as_evidence(tmp_path):
    """This discount is about the ABSENCE of measurement, not about failing it.
    A tag that was measured and did badly is handled by the regime multiplier,
    which has real numbers to work with."""
    j = Journal(tmp_path / "j.db")
    j.set_state("backtest:breakdown", '{"mean_alpha_r": -9.4, "regime": {}}')
    assert unvalidated_multiplier(j, "breakdown") == 1.0


def test_it_reaches_the_orchestrator_sizing_path(tmp_path, config):
    """End to end: the discount must actually shrink a proposal, not just exist."""
    from trading.guardrails.models import OrderProposal
    from trading.orchestrator import CycleReport, Orchestrator
    from stubs import StubBroker, make_account

    config.limits.portfolio.vol_target_annual = 0.15
    j = Journal(tmp_path / "j.db")
    j.record_snapshot(cycle="intraday", symbol="NVDA", bid=100, ask=100, last=100,
                      spread_bps=1.0, features={"realized_vol": 0.20},
                      sentiment=None, mention_count=None)
    orch = Orchestrator(config, j, StubBroker(make_account(equity=100_000)), client=None)

    draft = OrderProposal(symbol="NVDA", side="buy", qty=1000, order_type="limit",
                          limit_price=100.0, expected_edge_usd=100.0,
                          strategy_tag="vol-premium")
    orch._risk_size(draft, make_account(equity=100_000), CycleReport(cycle="intraday"))
    unvalidated_qty = draft.qty

    j.set_state("backtest:vol-premium", '{"mean_alpha_r": 0.0, "regime": {}}')
    draft2 = OrderProposal(symbol="NVDA", side="buy", qty=1000, order_type="limit",
                           limit_price=100.0, expected_edge_usd=100.0,
                           strategy_tag="vol-premium")
    orch._risk_size(draft2, make_account(equity=100_000), CycleReport(cycle="intraday"))

    assert unvalidated_qty < draft2.qty
    assert unvalidated_qty == pytest.approx(draft2.qty * UNVALIDATED_MULT, rel=0.05)

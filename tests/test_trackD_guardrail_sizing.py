"""Executable spec for the META calibration case: the fix for a max-loss veto is
to SIZE TO THE CAP, not to weaken the risk cap.

Concrete case: a META 645/655 debit call vertical at ~4.8 net debit. At 2
contracts the computed max loss (~$960) blows the $500 options cap; at 1
contract (~$480) it fits and is defined-risk. The guardrail engine already
computes this via `analyze_option_legs` — this test pins the arithmetic the
strategy agent must respect when choosing contract count.
"""

from datetime import date

from trading.guardrails.account_math import analyze_option_legs
from trading.guardrails.models import OptionLeg

CAP_USD = 500.0  # config: options.max_loss_per_trade_usd


def _vertical(contracts: int) -> list[OptionLeg]:
    """META 645/655 debit call vertical: long 645 @ 8.00, short 655 @ 3.20 =>
    net debit 4.80/share => max loss 4.80 * 100 * contracts."""
    expiry = date(2026, 8, 21)
    return [
        OptionLeg(side="buy", right="call", strike=645, expiry=expiry,
                  qty=contracts, est_premium=8.00),
        OptionLeg(side="sell", right="call", strike=655, expiry=expiry,
                  qty=contracts, est_premium=3.20),
    ]


def test_two_contracts_exceed_cap():
    a = analyze_option_legs(_vertical(2))
    assert a.is_defined_risk
    assert abs(a.max_loss_usd - 960.0) < 1e-6
    assert a.max_loss_usd > CAP_USD  # -> risk agent VETOES: correct


def test_one_contract_fits_cap():
    a = analyze_option_legs(_vertical(1))
    assert a.is_defined_risk
    assert abs(a.max_loss_usd - 480.0) < 1e-6
    assert a.max_loss_usd <= CAP_USD  # size-to-cap resolves the veto


def test_largest_size_within_cap_is_one_contract():
    """The intended agent behavior: pick the largest contract count whose
    computed max loss stays <= the cap."""
    best = max(
        (n for n in range(1, 11)
         if analyze_option_legs(_vertical(n)).max_loss_usd <= CAP_USD),
        default=0,
    )
    assert best == 1

"""Passive long core: hold a baseline index position, run the signals on top.

Why this exists
---------------
The nightly sweep, 40,212 trades over 10 years, says every one of the five
strategies loses to an exposure-matched passive hold — worst in the calm uptrends
that make up most of the decade. Per-trade expectancy is positive net of friction,
so the signals are not broken; what is broken is the assumption that being FLAT is
the safe default. Over the first 8 trading days flat cost $63 in compute to realise
$83 gross.

The sweep's own conclusion was "be passively long in a confirmed uptrend and deploy
these signals when the tape is not trending". This module is the first half of that
sentence.

Deliberately deterministic
--------------------------
No LLM decides this. It is a target weight, a band, and a rebalance — the cheapest
possible way to hold beta, and immune to the re-deliberation loop that made the
overlay expensive. It runs once a day after the open.

It is also exempt from the event wall: a passive allocation is not a directional
bet on a print, and blocking it on FOMC days would mean it could essentially never
be established (the event calendar covers a large fraction of trading days).
"""

from __future__ import annotations

from dataclasses import dataclass

from ..config import Config
from ..data.journal import Journal
from ..guardrails.models import OrderProposal

# Not in the strategy registry on purpose: the registry is for signals that must
# earn size through the sweep. This is an allocation, and it must not be sized down
# to 25% by the `unproven` lifecycle stage.
CORE_TAG = "passive-core"


@dataclass
class CorePlan:
    symbol: str
    target_shares: int
    current_shares: int
    target_usd: float
    reason: str

    @property
    def delta(self) -> int:
        return self.target_shares - self.current_shares

    @property
    def acts(self) -> bool:
        return self.delta != 0


def _regime_weight(journal: Journal, trend: str | None, vol_state: str | None) -> float:
    """Scale the core by how well passive beta does in this regime.

    Uses the same labels the overlay uses, so the two halves cannot disagree about
    what regime it is. Full weight in an uptrend, cut hard in a downtrend, half in
    anything unclassified -- the asymmetry is deliberate: the cost of holding beta
    in a drawdown is much larger than the cost of under-holding it in a rally.
    """
    if trend == "up":
        return 1.0
    if trend == "down":
        return 0.25
    if trend == "sideways":
        return 0.6
    return 0.5


def plan_core_position(config: Config, journal: Journal, account, price: float,
                       *, trend: str | None = None,
                       vol_state: str | None = None) -> CorePlan | None:
    """Target core holding, or None when the core is off / unpriceable."""
    cp = config.limits.core_position
    if not cp.enabled or cp.target_pct <= 0 or price <= 0 or account.equity <= 0:
        return None

    weight = _regime_weight(journal, trend, vol_state)
    target_usd = account.equity * (cp.target_pct / 100.0) * weight

    # The core must not eat the overlay's room. Cap it as a share of the gross
    # exposure budget, so adding a core never starves the signals it complements.
    gross_cap = account.equity * (config.limits.portfolio.max_gross_exposure_pct / 100.0)
    target_usd = min(target_usd, gross_cap * (cp.max_share_of_gross_pct / 100.0))

    current = 0
    for pos in (account.positions or []):
        if pos.symbol.upper() == cp.symbol.upper():
            current = int(pos.qty)
            break

    target_shares = int(target_usd / price)
    current_usd = current * price
    band = account.equity * (cp.rebalance_band_pct / 100.0)
    if abs(target_usd - current_usd) < band:
        # Inside the band: hold. Rebalancing on every wobble is how a passive core
        # turns into an expensive active one.
        target_shares = current

    return CorePlan(
        symbol=cp.symbol.upper(),
        target_shares=target_shares,
        current_shares=current,
        target_usd=target_usd,
        reason=(f"core {cp.target_pct:g}% x regime {trend or '?'}/{vol_state or '?'} "
                f"({weight:.2f}) = ${target_usd:,.0f}"),
    )


def core_proposal(plan: CorePlan, price: float) -> OrderProposal | None:
    """Turn a plan into a proposal for the normal guardrail pipeline.

    Goes through every deterministic guardrail like anything else -- notional caps,
    gross exposure, kill switch, reconciliation. Only the event wall and the
    strategy-lifecycle stage are bypassed, and both for the same reason: this is an
    allocation, not a signal making a claim about a catalyst.
    """
    if not plan.acts:
        return None
    delta = plan.delta
    side = "buy" if delta > 0 else "sell"
    return OrderProposal(
        agent="core",
        strategy_tag=CORE_TAG,
        symbol=plan.symbol,
        asset_class="stock",
        side=side,
        qty=abs(delta),
        order_type="limit",
        # Cross the spread by a hair: this is an allocation trade with no alpha
        # decay, but leaving it resting forever defeats the point of holding beta.
        limit_price=round(price * (1.002 if side == "buy" else 0.998), 2),
        reduces_position=(side == "sell"),
        thesis=(f"Passive core rebalance: {plan.reason}. Not a directional call -- "
                f"this is the book's default state, held so that 'no signal today' "
                f"means market exposure rather than cash."),
        expected_edge_usd=0.0,
        confidence=None,
    )

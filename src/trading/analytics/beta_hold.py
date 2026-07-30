"""Hold index beta in `up/calm`, and only there.

The evidence pointed here twice, from different directions. The first sweep said
no strategy beats an exposure-matched passive hold. Then four structurally
different bets -- trend-long, trend-short, mean-reversion and short-vol -- each
measured NEGATIVE in `up/calm` over roughly 3,000 trades apiece. That cell is the
most common of the decade and had one of eight strategies available, the one with
no backtest at all.

The reason is mechanical, not a missing signal. The benchmark in that cell is
being long, so a rule that sits in cash part of the time loses to it by
construction. Selectivity is the mistake there. No fifth signal fixes that; the
only thing that beats "be long" in a quiet uptrend is being long.

Deliberately narrower than the passive core that preceded it:

* **One regime.** Active only while the tape is `up/calm`. In every other cell the
  target is zero and the position is closed, so this never competes with the
  overlay for capital in the regimes where the overlay has measured edge.
* **No LLM.** A target weight, a band, and a rebalance. Nothing to re-deliberate.
* **Not exempt from anything.** Unlike the passive core, this carries no carve-out
  from the event wall, the lifecycle stage or the cost hurdle. It is an ordinary
  proposal through the ordinary guardrails.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..config import Config
from ..data.journal import Journal
from ..guardrails.models import OrderProposal

TAG = "beta-hold"
ACTIVE_REGIME = ("up", "calm")


@dataclass
class BetaPlan:
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


def plan_beta_hold(config: Config, journal: Journal, account, price: float, *,
                   trend: str | None, vol_state: str | None) -> BetaPlan | None:
    """Target index holding for the current regime, or None when disabled."""
    bh = config.limits.beta_hold
    if not bh.enabled or bh.target_pct <= 0 or price <= 0 or account.equity <= 0:
        return None

    in_regime = (trend, vol_state) == ACTIVE_REGIME
    target_usd = account.equity * (bh.target_pct / 100.0) if in_regime else 0.0

    current = 0
    for pos in (account.positions or []):
        if pos.symbol.upper() == bh.symbol.upper():
            current = int(pos.qty)
            break

    target_shares = int(target_usd / price)
    band = account.equity * (bh.rebalance_band_pct / 100.0)
    # Leaving the regime is not a drift; exit immediately rather than waiting for
    # the band. The whole thesis is that this edge exists in one cell only.
    if in_regime and abs(target_usd - current * price) < band:
        target_shares = current

    return BetaPlan(
        symbol=bh.symbol.upper(),
        target_shares=target_shares,
        current_shares=current,
        target_usd=target_usd,
        reason=(f"regime {trend or '?'}/{vol_state or '?'}"
                + (f" is up/calm -> hold ${target_usd:,.0f}" if in_regime
                   else " is not up/calm -> flat")),
    )


def beta_proposal(plan: BetaPlan, price: float) -> OrderProposal | None:
    if not plan.acts:
        return None
    delta = plan.delta
    side = "buy" if delta > 0 else "sell"
    return OrderProposal(
        agent="beta",
        strategy_tag=TAG,
        symbol=plan.symbol,
        asset_class="stock",
        side=side,
        qty=abs(delta),
        order_type="limit",
        limit_price=round(price * (1.002 if side == "buy" else 0.998), 2),
        reduces_position=(side == "sell"),
        thesis=(f"Beta hold: {plan.reason}. In a quiet uptrend the exposure-matched "
                f"benchmark IS being long -- four different signal families measure "
                f"negative alpha in this cell, so the edge is holding rather than "
                f"selecting."),
        # The equity risk premium over the holding period, not a per-trade alpha
        # claim. Deliberately modest so the cost hurdle is a real check.
        expected_edge_usd=abs(delta) * price * 0.01,
        confidence=None,
    )

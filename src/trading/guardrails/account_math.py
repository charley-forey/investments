"""Deterministic account math: position sizing, cost model, options-leg risk
analysis, and after-tax expectancy. Pure functions — unit-tested against
hand-computed examples; the LLM never does this arithmetic."""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from ..broker.models import AccountState, Quote
from ..config import CostHurdle, TaxRates
from .models import OptionLeg, OrderProposal

CONTRACT_MULTIPLIER = 100


# -- position sizing ----------------------------------------------------------

def size_stock_position(
    *,
    equity: float,
    risk_per_trade_pct: float,
    entry_price: float,
    stop_price: float,
    max_position_usd: float,
    max_position_pct: float,
) -> int:
    """Risk-based share count: risk budget / per-share risk, clamped by the
    position caps. Returns 0 when the trade can't be sized safely."""
    if entry_price <= 0 or stop_price <= 0:
        return 0
    per_share_risk = abs(entry_price - stop_price)
    if per_share_risk <= 0:
        return 0
    risk_budget = equity * risk_per_trade_pct / 100.0
    shares = math.floor(risk_budget / per_share_risk)
    cap_usd = min(max_position_usd, equity * max_position_pct / 100.0)
    max_shares_by_cap = math.floor(cap_usd / entry_price)
    return max(0, min(shares, max_shares_by_cap))


def vol_target_size(
    *, equity: float, entry_price: float, annual_vol: float,
    target_annual_vol: float, max_position_usd: float, max_position_pct: float,
) -> int:
    """Size a position so its standalone volatility contributes about
    `target_annual_vol` of equity. Higher-vol names get smaller positions.
    Returns a share count clamped by the position caps."""
    if entry_price <= 0 or annual_vol <= 0 or target_annual_vol <= 0 or equity <= 0:
        return 0
    # target $ vol = equity * target; position $ = target$vol / asset_vol
    target_dollar_vol = equity * target_annual_vol
    position_dollars = target_dollar_vol / annual_vol
    cap_usd = min(max_position_usd, equity * max_position_pct / 100.0)
    position_dollars = min(position_dollars, cap_usd)
    return max(0, math.floor(position_dollars / entry_price))


def kelly_fraction(win_rate: float, win_loss_ratio: float, *, cap: float = 0.25) -> float:
    """Fractional-Kelly bet size as a fraction of equity. Kelly f* = W - (1-W)/R.
    Capped (default quarter-Kelly ceiling) because full Kelly is too aggressive for
    real, estimation-error-prone edges. Never returns negative (no edge -> 0)."""
    if win_loss_ratio <= 0:
        return 0.0
    f = win_rate - (1.0 - win_rate) / win_loss_ratio
    if f <= 0:
        return 0.0
    return min(f, cap)


# -- cost model ---------------------------------------------------------------

def friction_cost(notional_usd: float, spread_usd: float, slippage_bps: float) -> float:
    """Core stock friction model, shared by live guardrails and the backtester so
    backtests never use a rosier cost assumption than production."""
    slippage = notional_usd * slippage_bps / 10_000.0
    return spread_usd + slippage


def estimate_cost_usd(
    proposal: OrderProposal,
    quote: Quote,
    hurdle: CostHurdle,
    option_leg_spreads: dict[str, float] | None = None,
) -> float:
    """Estimated round-trip friction: half-spread each way (= full spread once),
    regulatory/exchange fees, and slippage on notional.

    `option_leg_spreads` maps an OCC symbol to that leg's real per-share bid/ask
    spread; when provided it replaces the premium-based approximation for
    accurate hurdle checks."""
    if proposal.is_option:
        contracts = sum(l.qty for l in proposal.legs)
        spread_cost = 0.0
        for leg in proposal.legs:
            real = (option_leg_spreads or {}).get(leg.occ_symbol or "")
            per_share = real if real is not None else 0.04 * leg.est_premium
            spread_cost += per_share * leg.qty * CONTRACT_MULTIPLIER
        fees = hurdle.option_fee_per_contract_usd * contracts * 2  # open + close
        notional = sum(l.est_premium * l.qty * CONTRACT_MULTIPLIER for l in proposal.legs)
    else:
        spread_cost = quote.spread * proposal.qty
        fees = 0.0
        notional = (proposal.limit_price or quote.mid) * proposal.qty
    return fees + friction_cost(notional, spread_cost, hurdle.slippage_bps)


def proposal_notional_usd(proposal: OrderProposal, quote: Quote) -> float:
    if proposal.is_option:
        return sum(l.est_premium * l.qty * CONTRACT_MULTIPLIER for l in proposal.legs)
    return (proposal.limit_price or quote.mid) * proposal.qty


# -- options leg analysis -----------------------------------------------------

@dataclass
class OptionsAnalysis:
    is_defined_risk: bool
    max_loss_usd: float
    notes: list[str] = field(default_factory=list)


def analyze_option_legs(
    legs: list[OptionLeg],
    *,
    underlying_shares_held: float = 0.0,
    cash_available: float = 0.0,
) -> OptionsAnalysis:
    """Independently recompute worst-case loss from the legs and decide whether
    the structure is defined-risk. Per-pair math, conservative where ambiguous:

    - short paired 1:1 with a long of the same right (long expiry >= short expiry):
      debit-style pair (long is the protective strike): max loss = net premium paid;
      credit-style pair: max loss = strike width x 100 (credit received is ignored)
    - unpaired long option: max loss = premium paid
    - unpaired short call: allowed only as a covered call (>=100 shares/contract
      held); contributes 0 additional option-side loss
    - unpaired short put: allowed only cash-secured; max loss = strike x 100
    - anything else: naked / undefined risk
    """
    notes: list[str] = []
    max_loss = 0.0
    defined = True

    for right in ("call", "put"):
        longs: list[list] = []   # [strike, expiry, remaining_qty, est_premium]
        shorts: list[list] = []
        for leg in legs:
            if leg.right != right:
                continue
            target = longs if leg.side == "buy" else shorts
            target.append([leg.strike, leg.expiry, leg.qty, leg.est_premium])

        # Pair shorts against longs (long must not expire before the short).
        for s_strike, s_exp, s_qty, s_prem in shorts:
            remaining = s_qty
            for l in longs:
                if remaining <= 0:
                    break
                l_strike, l_exp, l_qty, l_prem = l[0], l[1], l[2], l[3]
                if l_qty <= 0 or l_exp < s_exp:
                    continue
                pair_qty = min(remaining, l_qty)
                is_debit_style = (
                    l_strike <= s_strike if right == "call" else l_strike >= s_strike
                )
                if is_debit_style:
                    pair_loss = max(0.0, l_prem - s_prem) * CONTRACT_MULTIPLIER * pair_qty
                else:
                    width = abs(s_strike - l_strike)
                    pair_loss = width * CONTRACT_MULTIPLIER * pair_qty
                max_loss += pair_loss
                l[2] -= pair_qty
                remaining -= pair_qty

            if remaining > 0:
                if right == "call":
                    needed = remaining * CONTRACT_MULTIPLIER
                    if underlying_shares_held >= needed:
                        underlying_shares_held -= needed
                        notes.append(f"covered call x{remaining} (shares held)")
                    else:
                        defined = False
                        notes.append(f"naked short call x{remaining} — not covered by shares")
                else:
                    needed_cash = s_strike * remaining * CONTRACT_MULTIPLIER
                    if cash_available >= needed_cash:
                        cash_available -= needed_cash
                        max_loss += needed_cash
                        notes.append(f"cash-secured put x{remaining} (max loss = strike value)")
                    else:
                        defined = False
                        notes.append(f"naked short put x{remaining} — not cash-secured")

        # Remaining unpaired long legs risk their premium.
        for _strike, _exp, l_qty, l_prem in longs:
            if l_qty > 0:
                max_loss += l_prem * l_qty * CONTRACT_MULTIPLIER

    return OptionsAnalysis(is_defined_risk=defined, max_loss_usd=max_loss, notes=notes)


# -- after-tax expectancy -----------------------------------------------------

def after_tax_pnl(pnl_usd: float, term: str, rates: TaxRates) -> float:
    """Net-of-tax P&L for a realized trade. Losses are credited at the same rate
    (approximation of offsetting gains elsewhere)."""
    rate = rates.long_term_total if term == "long" else rates.short_term_total
    return pnl_usd * (1.0 - rate)


def after_tax_expectancy(
    trades: list[tuple[float, str]], rates: TaxRates
) -> float:
    """Average after-tax P&L per trade. `trades` is [(pnl_usd, term), ...]."""
    if not trades:
        return 0.0
    total = sum(after_tax_pnl(pnl, term, rates) for pnl, term in trades)
    return total / len(trades)


def account_snapshot_summary(state: AccountState) -> str:
    """Human/agent-readable one-block summary of the deterministic account state."""
    lines = [
        f"mode={state.mode} equity=${state.equity:,.2f} cash=${state.cash:,.2f} "
        f"buying_power=${state.buying_power:,.2f}",
        f"daily P&L: ${state.daily_pl:,.2f} ({state.daily_pl_pct:+.2f}%)",
        f"day trades (5d): {state.daytrade_count}  PDT flagged: {state.pattern_day_trader}",
        f"open positions: {state.open_position_count}",
    ]
    for p in state.positions:
        lines.append(
            f"  {p.symbol}: {p.qty:g} @ ${p.avg_entry_price:,.2f} "
            f"(mv ${p.market_value:,.2f}, unrealized ${p.unrealized_pl:,.2f})"
        )
    for lot in state.lots:
        lines.append(
            f"  lot#{lot.lot_id} {lot.symbol} x{lot.qty:g} held {lot.holding_days}d "
            f"({'LONG-TERM' if lot.days_to_long_term == 0 else str(lot.days_to_long_term) + 'd to long-term'})"
        )
    return "\n".join(lines)

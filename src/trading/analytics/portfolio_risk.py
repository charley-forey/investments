"""Portfolio-level risk aggregation: roll a mixed stock + option book up into the
few numbers a portfolio guardrail actually checks — net delta-dollars, beta,
net vega, and gross/net leverage.

Pure and defensive (like options.py): every position field is pulled with
getattr/dict fallbacks so it works with PositionView, live snapshots, or test
stubs, and degrades to 0 rather than raising on bad input.

Position shape (all optional except qty):
    qty          signed share/contract count
    price        stock price, or option premium (per share)
    asset_class  'stock' | 'option'   (default 'stock')
    delta, vega  per-contract greeks (options)
    underlier    underlier SPOT PRICE (options) — what delta-dollars need
"""

from __future__ import annotations

from dataclasses import dataclass

CONTRACT_MULTIPLIER = 100  # mirrors guardrails.account_math


def _get(pos, name, default=None):
    """Attribute or dict-key lookup; None/missing -> default."""
    if isinstance(pos, dict):
        v = pos.get(name, default)
    else:
        v = getattr(pos, name, default)
    return default if v is None else v


def _num(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _is_option(pos) -> bool:
    return str(_get(pos, "asset_class", "stock")).lower() == "option"


def _greek(pos, sym, name, greeks):
    """Per-position greek, overridable by a greeks={symbol: {...}/obj} mapping."""
    if greeks and sym in greeks:
        g = greeks[sym]
        v = g.get(name) if isinstance(g, dict) else getattr(g, name, None)
        if v is not None:
            return _num(v)
    return _num(_get(pos, name, 0.0))


@dataclass
class PortfolioRisk:
    net_delta_dollars: float   # signed $ exposure to a 1-unit move (stock + option delta)
    portfolio_beta: float      # beta-adjusted net exposure / equity (dimensionless, ~1 = one long book)
    net_vega: float            # signed $ change per 1 vol-point (options)
    gross_exposure: float      # sum |market value|
    net_exposure: float        # sum signed market value
    gross_leverage: float      # gross / equity
    net_leverage: float        # net / equity
    largest_position_pct: float  # biggest |market value| as fraction of equity
    n_positions: int

    def summary(self) -> str:
        return (
            f"net delta ${self.net_delta_dollars:,.0f} | beta {self.portfolio_beta:.2f} | "
            f"net vega ${self.net_vega:,.0f} | gross {self.gross_leverage:.2f}x "
            f"net {self.net_leverage:.2f}x | "
            f"largest {self.largest_position_pct*100:.1f}% | {self.n_positions} pos"
        )


def portfolio_risk(positions, equity: float, *, betas=None, greeks=None) -> PortfolioRisk:
    """Aggregate a mixed book. `betas`/`greeks` are optional {symbol: ...} overrides;
    unknown beta defaults to 1.0. Never raises."""
    equity = _num(equity, 0.0)
    betas = betas or {}
    net_delta = net_vega = gross = net = beta_dollars = 0.0
    largest = 0.0

    for pos in positions or []:
        sym = str(_get(pos, "symbol", ""))
        qty = _num(_get(pos, "qty", 0.0))
        price = _num(_get(pos, "price", 0.0))

        if _is_option(pos):
            spot = _num(_get(pos, "underlier", 0.0))
            delta = _greek(pos, sym, "delta", greeks)
            vega = _greek(pos, sym, "vega", greeks)
            net_delta += delta * CONTRACT_MULTIPLIER * qty * spot
            net_vega += vega * CONTRACT_MULTIPLIER * qty
            mv = qty * price * CONTRACT_MULTIPLIER  # premium notional (signed)
        else:
            net_delta += qty * price
            mv = qty * price

        beta = _num(betas.get(sym, 1.0), 1.0) if sym in betas else 1.0
        beta_dollars += mv * beta
        gross += abs(mv)
        net += mv
        largest = max(largest, abs(mv))

    e = equity if equity > 0 else 1.0  # guard divide-by-zero; leverage 0 when no equity
    scale = 0.0 if equity <= 0 else 1.0
    return PortfolioRisk(
        net_delta_dollars=round(net_delta, 2),
        portfolio_beta=round(beta_dollars / e * scale, 4),
        net_vega=round(net_vega, 2),
        gross_exposure=round(gross, 2),
        net_exposure=round(net, 2),
        gross_leverage=round(gross / e * scale, 4),
        net_leverage=round(net / e * scale, 4),
        largest_position_pct=round(largest / e * scale, 4),
        n_positions=len(positions or []),
    )

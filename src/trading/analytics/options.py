"""Options analytics: turn a raw Alpaca option chain into decision-grade signals —
greeks, implied vol, moneyness, liquidity, and chain-level reads (ATM IV, IV vs
realized vol, put/call skew) plus IV rank from stored history.

Pure and defensive: every field is pulled with getattr fallbacks so it works with
both live Alpaca snapshot objects and lightweight test stubs, and degrades to None
when the data plan omits greeks/IV rather than raising."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from ..broker.occ import parse_occ


def _first(obj, *names):
    """First non-None attribute among names (handles nested objects flatly)."""
    for n in names:
        v = getattr(obj, n, None)
        if v is not None:
            return v
    return None


@dataclass
class ContractRow:
    occ: str
    expiry: date
    right: str            # 'call' | 'put'
    strike: float
    dte: int
    moneyness: float      # (strike - spot) / spot
    bid: float
    ask: float
    mid: float
    spread_bps: float | None
    iv: float | None
    delta: float | None
    gamma: float | None
    theta: float | None
    vega: float | None
    last_size: float | None  # size of the last trade (liquidity hint, not daily volume)

    def line(self) -> str:
        f = lambda v, s="{:.3f}": (s.format(v) if v is not None else "—")
        return (f"{self.expiry} {self.right:<4} {self.strike:<8.2f} "
                f"b{self.bid:<6.2f} a{self.ask:<6.2f} "
                f"iv {f(self.iv, '{:.1%}'):<6} d {f(self.delta):<7} "
                f"th {f(self.theta):<7} ve {f(self.vega):<7} "
                f"dte {self.dte:<3} {self.occ}")


def contract_row(occ: str, snap, underlying: str, spot: float,
                 today: date | None = None) -> ContractRow | None:
    """Build a ContractRow from an OCC symbol + Alpaca snapshot. None if unparseable."""
    try:
        parts = parse_occ(occ, underlying=underlying)
    except ValueError:
        return None
    today = today or date.today()
    q = getattr(snap, "latest_quote", None)
    bid = float(_first(q, "bid_price") or 0) if q is not None else 0.0
    ask = float(_first(q, "ask_price") or 0) if q is not None else 0.0
    mid = (bid + ask) / 2 if (bid > 0 and ask > 0) else (ask or bid)
    spread_bps = ((ask - bid) / mid * 10000) if (bid > 0 and ask > 0 and mid) else None
    greeks = getattr(snap, "greeks", None)
    trade = getattr(snap, "latest_trade", None)
    return ContractRow(
        occ=occ, expiry=parts.expiry, right=parts.right, strike=parts.strike,
        dte=(parts.expiry - today).days,
        moneyness=((parts.strike - spot) / spot) if spot else 0.0,
        bid=bid, ask=ask, mid=mid, spread_bps=spread_bps,
        iv=_num(_first(snap, "implied_volatility", "iv")),
        delta=_num(getattr(greeks, "delta", None)) if greeks is not None else None,
        gamma=_num(getattr(greeks, "gamma", None)) if greeks is not None else None,
        theta=_num(getattr(greeks, "theta", None)) if greeks is not None else None,
        vega=_num(getattr(greeks, "vega", None)) if greeks is not None else None,
        last_size=_num(getattr(trade, "size", None)) if trade is not None else None,
    )


def _num(v):
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def chain_rows(chain: dict, underlying: str, spot: float, *, max_dte: int = 60,
               min_dte: int = 0, moneyness_band: float = 0.10,
               today: date | None = None) -> list[ContractRow]:
    """Parse + filter a chain to near-the-money contracts within the DTE window.
    Ordered ATM-first (then nearest expiry) so the tradeable, greek-populated
    contracts lead — deep-ITM and 0-DTE strikes often carry no greeks."""
    today = today or date.today()
    rows = []
    for occ, snap in (chain or {}).items():
        row = contract_row(occ, snap, underlying, spot, today)
        if row is None or row.dte < min_dte or row.dte > max_dte:
            continue
        if spot > 0 and abs(row.moneyness) > moneyness_band:
            continue
        rows.append(row)
    rows.sort(key=lambda r: (round(abs(r.moneyness), 4), r.expiry, r.right, r.strike))
    return rows


@dataclass
class ChainSignals:
    atm_iv: float | None
    realized_vol: float | None
    iv_vs_rv: float | None        # atm_iv - realized_vol (>0 => options rich)
    richness: str                 # 'rich' | 'cheap' | 'fair' | 'unknown'
    pc_skew: float | None         # ATM put IV - ATM call IV (>0 => downside bid)
    atm_spread_bps: float | None  # tightest ATM spread (liquidity)
    n_contracts: int

    def summary(self) -> str:
        f = lambda v, s="{:.1%}": (s.format(v) if v is not None else "—")
        return (f"ATM IV {f(self.atm_iv)} vs realized {f(self.realized_vol)} "
                f"=> {self.richness} (IV-RV {f(self.iv_vs_rv)}); "
                f"put/call skew {f(self.pc_skew)}; "
                f"ATM spread {('%.0fbps' % self.atm_spread_bps) if self.atm_spread_bps is not None else '—'}; "
                f"{self.n_contracts} contracts")


def _atm(rows: list[ContractRow], right: str) -> ContractRow | None:
    cand = [r for r in rows if r.right == right and r.iv is not None]
    return min(cand, key=lambda r: abs(r.moneyness)) if cand else None


def chain_signals(rows: list[ContractRow], realized_vol: float | None) -> ChainSignals:
    """Chain-level read: ATM IV, IV-vs-realized richness, put/call skew, liquidity."""
    atm_call, atm_put = _atm(rows, "call"), _atm(rows, "put")
    ivs = [r.iv for r in (atm_call, atm_put) if r and r.iv is not None]
    atm_iv = sum(ivs) / len(ivs) if ivs else None
    iv_vs_rv = (atm_iv - realized_vol) if (atm_iv is not None and realized_vol is not None) else None
    if iv_vs_rv is None:
        richness = "unknown"
    elif iv_vs_rv > 0.05:
        richness = "rich"
    elif iv_vs_rv < -0.05:
        richness = "cheap"
    else:
        richness = "fair"
    pc_skew = (atm_put.iv - atm_call.iv) if (atm_put and atm_call
                                             and atm_put.iv is not None
                                             and atm_call.iv is not None) else None
    atm_spreads = [r.spread_bps for r in (atm_call, atm_put) if r and r.spread_bps is not None]
    atm_spread_bps = min(atm_spreads) if atm_spreads else None
    return ChainSignals(atm_iv=atm_iv, realized_vol=realized_vol, iv_vs_rv=iv_vs_rv,
                        richness=richness, pc_skew=pc_skew, atm_spread_bps=atm_spread_bps,
                        n_contracts=len(rows))


def iv_rank(current_iv: float | None, history: list[float]) -> float | None:
    """Percentile (0..100) of current ATM IV within its trailing history. Needs a few
    points to be meaningful; None if insufficient history or no current IV."""
    hist = [h for h in history if h is not None]
    if current_iv is None or len(hist) < 5:
        return None
    below = sum(1 for h in hist if h <= current_iv)
    return round(100.0 * below / len(hist), 1)

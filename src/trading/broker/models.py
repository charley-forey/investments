"""Broker-facing data models. AccountState is the deterministic snapshot every
agent cycle and every guardrail check starts from — the LLM never estimates these."""

from __future__ import annotations

from pydantic import BaseModel


class PositionView(BaseModel):
    symbol: str
    qty: float
    avg_entry_price: float
    market_value: float
    unrealized_pl: float
    asset_class: str = "stock"  # 'stock' | 'option'


class LotView(BaseModel):
    lot_id: int
    symbol: str
    qty: float
    open_price: float
    holding_days: int
    days_to_long_term: int  # 0 when already long-term


class AccountState(BaseModel):
    mode: str                      # 'paper' | 'live'
    equity: float
    cash: float
    buying_power: float
    last_equity: float             # equity at previous close (for daily P&L)
    daytrade_count: int            # broker's rolling 5-day day-trade counter
    pattern_day_trader: bool
    positions: list[PositionView] = []
    lots: list[LotView] = []

    @property
    def daily_pl(self) -> float:
        return self.equity - self.last_equity

    @property
    def daily_pl_pct(self) -> float:
        if self.last_equity <= 0:
            return 0.0
        return 100.0 * self.daily_pl / self.last_equity

    @property
    def open_position_count(self) -> int:
        return len(self.positions)

    def position_for(self, symbol: str) -> PositionView | None:
        symbol = symbol.upper()
        for p in self.positions:
            if p.symbol == symbol:
                return p
        return None


# A quote wider than this is not a real market on our large-cap universe — it is a
# thin-venue artifact. Measured on IEX 2026-07-24 during the core session: SPY 601bps,
# COST 1021bps, UNH 1067bps, while CRM/NVDA/AAPL returned ask=0. Real spreads on these
# names are 1-2bps, so this threshold cleanly separates artifact from market and goes
# permanently dormant on a consolidated (SIP) feed, where it never trips.
MAX_PLAUSIBLE_SPREAD_BPS = 100.0
# What we assume we actually pay when the book is unusable but a trade print exists.
ASSUMED_SPREAD_BPS = 5.0


class Quote(BaseModel):
    symbol: str
    bid: float
    ask: float
    bid_size: float = 0
    ask_size: float = 0
    last: float = 0.0  # last trade print; the only trustworthy price on a thin feed

    @property
    def book_is_usable(self) -> bool:
        """Two-sided, uncrossed, and tight enough to be a real market."""
        if not (self.bid > 0 and self.ask > 0 and self.ask >= self.bid):
            return False
        mid = (self.bid + self.ask) / 2
        return (self.ask - self.bid) / mid * 10_000 <= MAX_PLAUSIBLE_SPREAD_BPS

    @property
    def effective_book(self) -> tuple[float, float]:
        """The book to price against. Real quote when it is a real market; otherwise
        a tight synthetic book around the last trade — a stale/one-sided venue quote
        must never become the price we mark, size, or route on."""
        if self.book_is_usable:
            return self.bid, self.ask
        if self.last > 0:
            half = self.last * ASSUMED_SPREAD_BPS / 20_000
            return self.last - half, self.last + half
        return self.bid, self.ask

    @property
    def mid(self) -> float:
        bid, ask = self.effective_book
        if bid > 0 and ask > 0:
            return (bid + ask) / 2
        return ask or bid

    @property
    def is_two_sided(self) -> bool:
        """A book with a real price on both sides. IEX routinely returns ask=0
        outside its own liquidity, which is unquotable — not tight."""
        return self.book_is_usable or self.last > 0

    @property
    def spread(self) -> float:
        """Infinite only when we have neither a usable book nor a trade print: an
        unpriceable trade must fail the cost hurdle, never pass it as a perfect
        zero-spread fill. A bogus venue spread would fail it just as wrongly."""
        bid, ask = self.effective_book
        if bid > 0 and ask > 0:
            return max(ask - bid, 0.0)
        return float("inf")

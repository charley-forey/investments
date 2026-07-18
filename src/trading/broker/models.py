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


class Quote(BaseModel):
    symbol: str
    bid: float
    ask: float
    bid_size: float = 0
    ask_size: float = 0

    @property
    def mid(self) -> float:
        if self.bid > 0 and self.ask > 0:
            return (self.bid + self.ask) / 2
        return self.ask or self.bid

    @property
    def spread(self) -> float:
        if self.bid > 0 and self.ask > 0:
            return max(self.ask - self.bid, 0.0)
        return 0.0

"""Thin alpaca-py wrappers. This module is the ONLY place broker credentials are
used, and `submit_order` here is only ever called by the guardrail executor."""

from __future__ import annotations

from datetime import datetime, timezone

from ..config import Config
from ..data.journal import Journal
from .models import AccountState, LotView, PositionView, Quote

LONG_TERM_DAYS = 365


class AlpacaBroker:
    def __init__(self, config: Config):
        if not config.secrets.has_alpaca:
            mode = config.limits.mode
            raise RuntimeError(
                f"Missing Alpaca {mode} API keys - set ALPACA_{mode.upper()}_API_KEY / "
                f"ALPACA_{mode.upper()}_SECRET_KEY in .env"
            )
        self.config = config
        self._trading = None
        self._stock_data = None
        self._option_data = None

    # Lazy clients so importing this module never requires network/credentials.
    @property
    def trading(self):
        if self._trading is None:
            from alpaca.trading.client import TradingClient

            self._trading = TradingClient(
                self.config.secrets.alpaca_api_key,
                self.config.secrets.alpaca_secret_key,
                paper=not self.config.is_live,
            )
        return self._trading

    @property
    def stock_data(self):
        if self._stock_data is None:
            from alpaca.data.historical import StockHistoricalDataClient

            self._stock_data = StockHistoricalDataClient(
                self.config.secrets.alpaca_api_key,
                self.config.secrets.alpaca_secret_key,
            )
        return self._stock_data

    @property
    def option_data(self):
        if self._option_data is None:
            from alpaca.data.historical.option import OptionHistoricalDataClient

            self._option_data = OptionHistoricalDataClient(
                self.config.secrets.alpaca_api_key,
                self.config.secrets.alpaca_secret_key,
            )
        return self._option_data

    # -- market data ----------------------------------------------------------

    def get_quote(self, symbol: str) -> Quote:
        from alpaca.data.requests import StockLatestQuoteRequest

        symbol = symbol.upper()
        req = StockLatestQuoteRequest(symbol_or_symbols=symbol)
        q = self.stock_data.get_stock_latest_quote(req)[symbol]
        return Quote(
            symbol=symbol,
            bid=float(q.bid_price or 0),
            ask=float(q.ask_price or 0),
            bid_size=float(q.bid_size or 0),
            ask_size=float(q.ask_size or 0),
        )

    def get_option_quote(self, option_symbol: str) -> Quote:
        from alpaca.data.requests import OptionLatestQuoteRequest

        option_symbol = option_symbol.upper()
        req = OptionLatestQuoteRequest(symbol_or_symbols=option_symbol)
        q = self.option_data.get_option_latest_quote(req)[option_symbol]
        return Quote(
            symbol=option_symbol,
            bid=float(q.bid_price or 0),
            ask=float(q.ask_price or 0),
        )

    def get_bars(self, symbol: str, days: int = 30):
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame
        from datetime import timedelta

        req = StockBarsRequest(
            symbol_or_symbols=symbol.upper(),
            timeframe=TimeFrame.Day,
            start=datetime.now(timezone.utc) - timedelta(days=days * 2),
        )
        return self.stock_data.get_stock_bars(req).df

    def get_options_chain(self, underlying: str):
        from alpaca.data.requests import OptionChainRequest

        req = OptionChainRequest(underlying_symbol=underlying.upper())
        return self.option_data.get_option_chain(req)

    def market_open(self) -> bool:
        clock = self.trading.get_clock()
        return bool(clock.is_open)

    def get_news(self, symbol: str, limit: int = 10) -> list[dict]:
        from alpaca.data.historical.news import NewsClient
        from alpaca.data.requests import NewsRequest

        client = NewsClient(
            self.config.secrets.alpaca_api_key, self.config.secrets.alpaca_secret_key
        )
        req = NewsRequest(symbols=symbol.upper(), limit=limit)
        news = client.get_news(req)
        items = []
        for article in news.data.get("news", []):
            items.append(
                {
                    "headline": article.headline,
                    "summary": (article.summary or "")[:300],
                    "created_at": str(article.created_at),
                    "source": article.source,
                }
            )
        return items

    def list_orders_since(self, since: datetime):
        """Orders updated after `since` (for fill sync)."""
        from alpaca.trading.enums import QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest

        req = GetOrdersRequest(status=QueryOrderStatus.ALL, after=since, limit=500)
        return self.trading.get_orders(filter=req)

    # -- account snapshot -----------------------------------------------------

    def get_account_state(self, journal: Journal | None = None) -> AccountState:
        acct = self.trading.get_account()
        positions = []
        for p in self.trading.get_all_positions():
            positions.append(
                PositionView(
                    symbol=p.symbol,
                    qty=float(p.qty),
                    avg_entry_price=float(p.avg_entry_price or 0),
                    market_value=float(p.market_value or 0),
                    unrealized_pl=float(p.unrealized_pl or 0),
                    asset_class="option" if getattr(p, "asset_class", "") == "us_option" else "stock",
                )
            )

        lots: list[LotView] = []
        if journal is not None:
            now = datetime.now(timezone.utc)
            for lot in journal.open_lots():
                holding_days = (now - datetime.fromisoformat(lot["open_ts"])).days
                lots.append(
                    LotView(
                        lot_id=lot["id"],
                        symbol=lot["symbol"],
                        qty=lot["qty"],
                        open_price=lot["open_price"],
                        holding_days=holding_days,
                        days_to_long_term=max(0, LONG_TERM_DAYS + 1 - holding_days),
                    )
                )

        return AccountState(
            mode=self.config.limits.mode,
            equity=float(acct.equity or 0),
            cash=float(acct.cash or 0),
            buying_power=float(acct.buying_power or 0),
            last_equity=float(acct.last_equity or 0),
            daytrade_count=int(acct.daytrade_count or 0),
            pattern_day_trader=bool(acct.pattern_day_trader),
            positions=positions,
            lots=lots,
        )

    # -- orders (called ONLY by the guardrail executor) -----------------------

    def submit_order(
        self,
        *,
        symbol: str,
        side: str,
        qty: float,
        order_type: str,
        limit_price: float | None,
    ) -> str:
        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.trading.requests import LimitOrderRequest, MarketOrderRequest

        side_enum = OrderSide.BUY if side == "buy" else OrderSide.SELL
        if order_type == "limit":
            if limit_price is None:
                raise ValueError("limit order requires limit_price")
            req = LimitOrderRequest(
                symbol=symbol.upper(),
                qty=qty,
                side=side_enum,
                time_in_force=TimeInForce.DAY,
                limit_price=round(float(limit_price), 2),
            )
        else:
            req = MarketOrderRequest(
                symbol=symbol.upper(),
                qty=qty,
                side=side_enum,
                time_in_force=TimeInForce.DAY,
            )
        order = self.trading.submit_order(order_data=req)
        return str(order.id)

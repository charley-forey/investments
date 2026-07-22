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

    @property
    def _feed(self):
        """Market data feed. IEX (the free default) covers ~2% of volume and
        returns one-sided books and multi-percent spreads for liquid names, which
        the risk agent correctly but uselessly reads as 'do not enter'. SIP is the
        consolidated tape and requires an Algo Trader Plus subscription."""
        from alpaca.data.enums import DataFeed

        name = str(getattr(self.config.settings, "data_feed", "iex") or "iex").lower()
        return DataFeed.SIP if name == "sip" else DataFeed.IEX

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
        req = StockLatestQuoteRequest(symbol_or_symbols=symbol, feed=self._feed)
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

    def get_bars(self, symbol: str, days: int = 30, timeframe: str = "1Day"):
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
        from datetime import timedelta

        tf_map = {
            "1Day": TimeFrame.Day,
            "1Min": TimeFrame(1, TimeFrameUnit.Minute),
            "5Min": TimeFrame(5, TimeFrameUnit.Minute),
            "15Min": TimeFrame(15, TimeFrameUnit.Minute),
            "1Hour": TimeFrame(1, TimeFrameUnit.Hour),
        }
        tf = tf_map.get(timeframe, TimeFrame.Day)
        # Intraday: pull from the start of today's session; daily: a lookback window.
        if timeframe != "1Day":
            start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        else:
            start = datetime.now(timezone.utc) - timedelta(days=days * 2)
        req = StockBarsRequest(symbol_or_symbols=symbol.upper(), timeframe=tf,
                               start=start, feed=self._feed)
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
        stop_loss_price: float | None = None,
        take_profit_price: float | None = None,
        client_order_id: str | None = None,
    ) -> str:
        from alpaca.trading.enums import OrderClass, OrderSide, TimeInForce
        from alpaca.trading.requests import (
            LimitOrderRequest, MarketOrderRequest, StopLossRequest,
            StopOrderRequest, TakeProfitRequest,
        )

        side_enum = OrderSide.BUY if side == "buy" else OrderSide.SELL
        # A protective stop (optionally a target) turns this into an atomic bracket
        # so the position is protected the instant the entry fills.
        bracket_kwargs = {}
        if stop_loss_price is not None and order_type != "stop":
            bracket_kwargs["order_class"] = OrderClass.BRACKET
            bracket_kwargs["stop_loss"] = StopLossRequest(
                stop_price=round(float(stop_loss_price), 2)
            )
            if take_profit_price is not None:
                bracket_kwargs["take_profit"] = TakeProfitRequest(
                    limit_price=round(float(take_profit_price), 2)
                )

        # Standalone protective stops (e.g. re-attach after a bracket was cancelled)
        # use GTC so overnight protection survives the session.
        if order_type == "stop":
            stop_px = stop_loss_price if stop_loss_price is not None else limit_price
            if stop_px is None:
                raise ValueError("stop order requires stop_loss_price or limit_price")
            req = StopOrderRequest(
                symbol=symbol.upper(), qty=qty, side=side_enum,
                stop_price=round(float(stop_px), 2),
                time_in_force=TimeInForce.GTC, client_order_id=client_order_id,
            )
            order = self.trading.submit_order(order_data=req)
            return str(order.id)

        # Bracket children inherit the parent's TIF: a DAY bracket has its target
        # expire at the close, which cancels the sibling stop and leaves the
        # position naked overnight. GTC keeps protection alive; an unfilled GTC
        # entry is swept by cancel_stale_orders (orders.stale_order_ttl_minutes).
        tif = TimeInForce.GTC if bracket_kwargs else TimeInForce.DAY
        common = dict(symbol=symbol.upper(), qty=qty, side=side_enum,
                      time_in_force=tif, client_order_id=client_order_id)
        if order_type == "limit":
            if limit_price is None:
                raise ValueError("limit order requires limit_price")
            req = LimitOrderRequest(limit_price=round(float(limit_price), 2),
                                    **common, **bracket_kwargs)
        else:
            req = MarketOrderRequest(**common, **bracket_kwargs)
        order = self.trading.submit_order(order_data=req)
        return str(order.id)

    def list_open_orders(self):
        from alpaca.trading.enums import QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest

        req = GetOrdersRequest(status=QueryOrderStatus.OPEN, limit=500)
        return self.trading.get_orders(filter=req)

    def cancel_order(self, order_id: str) -> None:
        self.trading.cancel_order_by_id(order_id)

    def submit_option_order(
        self,
        *,
        legs: list[dict],
        net_limit_price: float | None,
        underlying: str,
        client_order_id: str | None = None,
    ) -> str:
        """Submit a single- or multi-leg option order. `legs` items:
        {occ_symbol, side ('buy'|'sell'), qty (contracts)}. A multi-leg order
        uses OrderClass.MLEG with a net limit price (positive = debit)."""
        from alpaca.trading.enums import (
            OrderClass, OrderSide, PositionIntent, TimeInForce,
        )
        from alpaca.trading.requests import (
            LimitOrderRequest, MarketOrderRequest, OptionLegRequest,
        )

        def _side(s: str) -> "OrderSide":
            return OrderSide.BUY if s == "buy" else OrderSide.SELL

        if len(legs) == 1:
            leg = legs[0]
            if net_limit_price is not None:
                req = LimitOrderRequest(
                    symbol=leg["occ_symbol"], qty=leg["qty"], side=_side(leg["side"]),
                    time_in_force=TimeInForce.DAY, client_order_id=client_order_id,
                    limit_price=round(abs(float(net_limit_price)), 2),
                )
            else:
                req = MarketOrderRequest(
                    symbol=leg["occ_symbol"], qty=leg["qty"], side=_side(leg["side"]),
                    time_in_force=TimeInForce.DAY, client_order_id=client_order_id,
                )
        else:
            order_legs = [
                OptionLegRequest(
                    symbol=leg["occ_symbol"],
                    side=_side(leg["side"]),
                    ratio_qty=leg["qty"],
                )
                for leg in legs
            ]
            req = LimitOrderRequest(
                qty=1,
                order_class=OrderClass.MLEG,
                time_in_force=TimeInForce.DAY,
                client_order_id=client_order_id,
                limit_price=round(float(net_limit_price or 0.0), 2),
                legs=order_legs,
            )
        order = self.trading.submit_order(order_data=req)
        return str(order.id)

"""Offline stubs for the Anthropic client and Alpaca broker, plus a scripted
agent builder. No network, no keys."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from trading.broker.models import AccountState, Quote


# -- broker stub --------------------------------------------------------------

class StubBroker:
    def __init__(self, account: AccountState, quotes: dict[str, Quote] | None = None,
                 market_open: bool = True, orders: list | None = None):
        self._account = account
        self._quotes = quotes or {}
        self._market_open = market_open
        self._orders = orders or []
        self.submitted: list[dict] = []

    def get_account_state(self, journal=None) -> AccountState:
        return self._account

    def get_quote(self, symbol: str) -> Quote:
        return self._quotes.get(symbol.upper(), Quote(symbol=symbol.upper(), bid=99.9, ask=100.1))

    def get_bars(self, symbol: str, days: int = 30, timeframe: str = "1Day"):
        return None

    def get_options_chain(self, symbol: str):
        return {}

    def get_news(self, symbol: str, limit: int = 10):
        return []

    def market_open(self) -> bool:
        return self._market_open

    def list_orders_since(self, since):
        # Mirror Alpaca: `after` filters on submitted_at (not updated_at).
        out = []
        for o in self._orders:
            submitted = getattr(o, "submitted_at", None)
            if submitted is None or since is None or submitted > since:
                out.append(o)
        return out

    def submit_order(self, *, symbol, side, qty, order_type, limit_price,
                     stop_loss_price=None, take_profit_price=None, client_order_id=None):
        self.submitted.append(dict(symbol=symbol, side=side, qty=qty,
                                   order_type=order_type, limit_price=limit_price,
                                   stop_loss_price=stop_loss_price,
                                   take_profit_price=take_profit_price,
                                   client_order_id=client_order_id))
        return f"stub-order-{len(self.submitted)}"

    def list_open_orders(self):
        return getattr(self, "_open_orders", [])

    def cancel_order(self, order_id):
        self.canceled = getattr(self, "canceled", [])
        self.canceled.append(order_id)


@dataclass
class StubOrder:
    id: str
    symbol: str
    side: str
    filled_qty: float
    filled_avg_price: float
    status: str = "filled"
    commission: float = 0.0
    client_order_id: str | None = None
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    submitted_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


# -- Anthropic client stub ----------------------------------------------------

class _Block:
    def __init__(self, type, **kw):
        self.type = type
        for k, v in kw.items():
            setattr(self, k, v)


class _Response:
    def __init__(self, content, stop_reason):
        self.content = content
        self.stop_reason = stop_reason


def text_block(t: str):
    return _Block("text", text=t)


def tool_use_block(tool_id: str, name: str, tool_input: dict):
    return _Block("tool_use", id=tool_id, name=name, input=tool_input)


class ScriptedClient:
    """Returns a queued list of responses in order. Each `messages.create` call
    pops the next scripted response, ignoring the actual messages."""

    def __init__(self, responses: list[_Response]):
        self._responses = list(responses)
        self.calls = 0
        self.messages = self  # so client.messages.create works

    def create(self, **kwargs):
        self.calls += 1
        if not self._responses:
            # Default: end the turn with empty text.
            return _Response([text_block("done")], "end_turn")
        return self._responses.pop(0)


def make_account(equity=50000.0, positions=None, daily_pl_pct=0.0) -> AccountState:
    last_equity = equity / (1 + daily_pl_pct / 100.0) if daily_pl_pct else equity
    return AccountState(
        mode="paper", equity=equity, cash=equity, buying_power=equity * 2,
        last_equity=last_equity, daytrade_count=0, pattern_day_trader=False,
        positions=positions or [],
    )

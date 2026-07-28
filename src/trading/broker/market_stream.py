"""Real-time market-data websocket: continuous detection, episodic reasoning.

The daemon samples the tape every 15 minutes. A level crossed at 10:02 and faded
by 10:12 was invisible, and `orb-breakout` / `vwap-mean-reversion` were labels the
scanner assigned from *daily* features because nothing held live session state.

This process watches every symbol in the universe tick by tick and writes a
`wake_events` row when something actually happens. The daemon's cost gate drains
that queue. Detection is continuous and costs no LLM tokens; only a real event
buys a billed call.

`SessionState` is deliberately pure — no broker, no clock, no I/O — so the
detection logic is testable without a websocket. `run_market_stream` is glue.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from ..config import Config
from ..data.journal import Journal

log = logging.getLogger("trading.market_stream")

ET = ZoneInfo("America/New_York")
MARKET_OPEN = time(9, 30)
OPENING_RANGE_MINUTES = 5
# One wake per symbol per kind per this many minutes. A stock chopping across its
# trigger would otherwise bill an LLM call on every tick.
DEBOUNCE_MINUTES = 30


@dataclass(frozen=True)
class WakeEvent:
    symbol: str
    kind: str            # 'trigger' | 'orb'
    detail: str
    price: float


@dataclass
class SessionState:
    """Live per-symbol session state, advanced one trade at a time.

    Holds what the 15-minute scanner never had: the opening range as it forms,
    a running VWAP, and the session extremes — so an ORB break is detected when
    it happens rather than inferred from a daily bar hours later.
    """

    symbol: str
    or_minutes: int = OPENING_RANGE_MINUTES
    triggers: list = field(default_factory=list)   # Trigger objects for this symbol

    or_high: float | None = None
    or_low: float | None = None
    session_high: float | None = None
    session_low: float | None = None
    last: float | None = None
    _pv: float = 0.0                                # sum(price * size), for VWAP
    _vol: float = 0.0
    _fired: dict = field(default_factory=dict)      # (kind, tag) -> datetime of last fire
    _session_date: object = None                    # date of the session in progress

    def reset_session(self, day) -> None:
        """Clear everything session-scoped. This process runs for days, so without
        it day 2's opening range would merge with day 1's and never break."""
        self._session_date = day
        self.or_high = self.or_low = None
        self.session_high = self.session_low = None
        self.last = None
        self._pv = self._vol = 0.0
        self._fired.clear()

    @property
    def vwap(self) -> float | None:
        return (self._pv / self._vol) if self._vol > 0 else None

    def _in_opening_range(self, ts_et: datetime) -> bool:
        open_dt = datetime.combine(ts_et.date(), MARKET_OPEN, tzinfo=ET)
        return open_dt <= ts_et < open_dt + timedelta(minutes=self.or_minutes)

    def _debounced(self, kind: str, tag: str, ts_et: datetime) -> bool:
        prev = self._fired.get((kind, tag))
        if prev and (ts_et - prev) < timedelta(minutes=DEBOUNCE_MINUTES):
            return True
        self._fired[(kind, tag)] = ts_et
        return False

    def on_trade(self, price: float, size: float, ts_et: datetime) -> list[WakeEvent]:
        """Advance the state and return any events this trade caused.

        Crossings are edge-triggered off the PREVIOUS price, so a symbol that
        opens already through its level does not fire — that is a standing fact,
        not an event, the same distinction the cost gate makes for scores."""
        if price <= 0:
            return []
        if self._session_date != ts_et.date():
            self.reset_session(ts_et.date())
        prev = self.last
        self.last = price
        self._pv += price * max(size, 0.0)
        self._vol += max(size, 0.0)
        self.session_high = price if self.session_high is None else max(self.session_high, price)
        self.session_low = price if self.session_low is None else min(self.session_low, price)

        if self._in_opening_range(ts_et):
            self.or_high = price if self.or_high is None else max(self.or_high, price)
            self.or_low = price if self.or_low is None else min(self.or_low, price)
            return []                                  # range still forming

        events: list[WakeEvent] = []
        if prev is None:
            return events                              # no edge to detect yet

        if self.or_high is not None and prev <= self.or_high < price:
            if not self._debounced("orb", "high", ts_et):
                events.append(WakeEvent(self.symbol, "orb",
                                        f"broke opening range high {self.or_high:g}", price))
        if self.or_low is not None and prev >= self.or_low > price:
            if not self._debounced("orb", "low", ts_et):
                events.append(WakeEvent(self.symbol, "orb",
                                        f"broke opening range low {self.or_low:g}", price))

        for t in self.triggers:
            level = float(getattr(t, "level", 0) or 0)
            direction = (getattr(t, "direction", "") or "").lower()
            if level <= 0:
                continue
            crossed = (prev <= level < price) if direction != "below" else (prev >= level > price)
            if crossed and not self._debounced("trigger", f"{direction}:{level}", ts_et):
                events.append(WakeEvent(self.symbol, "trigger",
                                        f"crossed {direction} {level:g}", price))
        return events


def _states_for(config: Config) -> dict[str, SessionState]:
    """One SessionState per universe symbol, preloaded with its watchlist triggers."""
    from ..scanner.universe import load_screen_universe
    from ..triggers import load_triggers

    symbols = sorted({s.upper() for s in load_screen_universe().all_symbols(config)})
    by_symbol: dict[str, list] = {}
    for t in load_triggers(config):
        by_symbol.setdefault(t.symbol.upper(), []).append(t)
    return {s: SessionState(symbol=s, triggers=by_symbol.get(s, [])) for s in symbols}


def run_market_stream(config: Config) -> None:
    """Blocking: stream trades for the whole universe and queue wake events.

    Fails safe by construction — if this process dies the daemon's 15-minute cron
    still runs, so the worst case is the latency we had before it existed."""
    from alpaca.data.enums import DataFeed
    from alpaca.data.live import StockDataStream

    journal = Journal(config.settings.paths.journal_db)
    states = _states_for(config)
    # DataFeed is a str enum, so a bare "sip" passes StockDataStream's membership
    # check and then dies on feed.value inside the constructor. Coerce it.
    name = str(getattr(config.settings, "data_feed", "iex") or "iex").lower()
    feed = DataFeed.SIP if name == "sip" else DataFeed.IEX
    log.info("market stream: %d symbols, feed=%s, OR=%dm, debounce=%dm",
             len(states), feed.value, OPENING_RANGE_MINUTES, DEBOUNCE_MINUTES)

    stream = StockDataStream(
        config.secrets.alpaca_api_key,
        config.secrets.alpaca_secret_key,
        feed=feed,
    )

    async def on_trade(t):
        try:
            state = states.get(str(t.symbol).upper())
            if state is None:
                return
            ts = t.timestamp.astimezone(ET)
            for ev in state.on_trade(float(t.price), float(getattr(t, "size", 0) or 0), ts):
                journal.record_wake_event(symbol=ev.symbol, kind=ev.kind,
                                          detail=ev.detail, price=ev.price)
                log.info("wake: %s %s @ %.2f (%s)", ev.symbol, ev.kind, ev.price, ev.detail)
                journal.heartbeat("market_stream", detail=f"{ev.symbol} {ev.kind}")
        except Exception:  # one bad tick must never kill the stream
            log.exception("tick handling failed for %s", getattr(t, "symbol", "?"))

    stream.subscribe_trades(on_trade, *states.keys())
    log.info("market stream connecting (%d symbols)", len(states))
    stream.run()

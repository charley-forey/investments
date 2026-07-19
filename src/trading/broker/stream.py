"""Real-time fill updates via Alpaca's TradingStream websocket.

On any trade update we trigger the (idempotent) polling sync — reusing the tested
lot/day-trade logic rather than duplicating it. Polling remains the reconciliation
backstop; this just makes fills land in the journal in seconds instead of at the
next scheduled cycle.
"""

from __future__ import annotations

import logging

from ..config import Config
from ..data.journal import Journal

log = logging.getLogger("trading.stream")


def run_trade_stream(config: Config) -> None:
    """Blocking: connect the trade-update websocket and sync on each event.
    Reconnection is handled by alpaca-py's TradingStream internally."""
    from alpaca.trading.stream import TradingStream

    from .alpaca import AlpacaBroker
    from .sync import sync_fills

    journal = Journal(config.settings.paths.journal_db)
    broker = AlpacaBroker(config)

    stream = TradingStream(
        config.secrets.alpaca_api_key,
        config.secrets.alpaca_secret_key,
        paper=not config.is_live,
    )

    async def on_trade_update(data):
        event = getattr(data, "event", "?")
        log.info("trade update: %s", event)
        try:
            report = sync_fills(config, journal, broker)
            if report.fills_recorded:
                journal.heartbeat("stream", detail=f"{event}: +{report.fills_recorded} fills")
        except Exception as e:  # a bad sync must not kill the stream
            log.exception("sync on trade update failed")
            journal.heartbeat("stream", status="error", detail=str(e))

    stream.subscribe_trade_updates(on_trade_update)
    log.info("trade stream connecting (mode=%s)", config.limits.mode)
    stream.run()

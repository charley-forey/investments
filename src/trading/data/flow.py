"""Options-flow provider seam. Institutional flow — sweeps, unusual OI/volume, dark
pool — is NOT available from Alpaca; it needs a paid feed (Unusual Whales, etc.). This
is the one-file plug point, mirroring the sentiment-provider seam in data/sentiment.py.

The default provider is a no-op (returns None), so callers never break with no feed
configured — the system relies on IV rank + put/call skew derived from our own stored
snapshot history instead. Drop in a real feed by subclassing FlowProvider and returning
it from get_flow_provider, with no change to callers."""

from __future__ import annotations

from dataclasses import dataclass

from ..config import Config


@dataclass
class FlowSignal:
    symbol: str
    put_call_ratio: float | None       # volume-based, from the feed
    unusual_score: float | None        # 0..1 unusual-activity strength
    net_premium_usd: float | None      # net directional premium (calls - puts)
    notes: str = ""

    def summary(self) -> str:
        if self.put_call_ratio is None and self.unusual_score is None:
            return f"{self.symbol}: no options-flow feed configured"
        return (f"{self.symbol}: P/C {self.put_call_ratio}, unusual {self.unusual_score}, "
                f"net premium {self.net_premium_usd}. {self.notes}")


class FlowProvider:
    """Pluggable options-flow backend."""

    def get_flow(self, config: Config, symbol: str) -> FlowSignal | None:
        raise NotImplementedError


class NullFlowProvider(FlowProvider):
    """Default: no external flow feed. Returns None so callers degrade to IV-rank +
    skew from our own snapshot history."""

    def get_flow(self, config: Config, symbol: str) -> FlowSignal | None:
        return None


def get_flow_provider(config: Config) -> FlowProvider:
    # Select a real provider here once a feed + credentials are configured; the
    # null provider is the safe default for tests and no-feed runs.
    return NullFlowProvider()

"""Options-flow (unusual activity) provider seam — BLOCKED on a paid feed.

Unusual options activity (sweeps, blocks, unusual volume) is a genuine additional
edge, but it needs a paid market-data entitlement. This mirrors the sentiment and
calendar provider seams so a real backend drops in later with no caller changes;
the default returns nothing, so nothing in the system depends on data we don't
have. Do NOT build strategy on top of this until a real provider is wired.

ponytail: seam only, no consumers — add the real provider + callers when a feed exists.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class FlowSignal:
    symbol: str
    kind: str            # 'sweep' | 'block' | 'unusual_volume'
    direction: str       # 'bullish' | 'bearish'
    notional_usd: float
    note: str = ""


class OptionsFlowProvider:
    """Pluggable options-flow backend. Subclass and return it from get_flow_provider
    to wire a real feed; callers only use unusual_activity."""

    available: bool = False

    def unusual_activity(self, symbol: str | None = None, *,
                         min_notional: float = 0.0) -> list[FlowSignal]:
        return []


class NullFlowProvider(OptionsFlowProvider):
    """Default: no feed configured, so no signals. Keeps callers safe/no-op."""


def get_flow_provider(config) -> OptionsFlowProvider:
    """Factory: the Null provider until a paid options-flow feed is credentialed
    here (read config.settings/secrets and return a real provider)."""
    return NullFlowProvider()

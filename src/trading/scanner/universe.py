"""Liquid screen-universe loader for the movers / opportunity scanner."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from ..config import PROJECT_ROOT, Config


@dataclass
class ScreenFilters:
    min_price: float = 5.0
    min_avg_daily_volume: int = 1_000_000
    max_spread_bps: float = 40.0
    top_n: int = 8
    ttl_hours: int = 36
    wake_score: float = 55.0


@dataclass
class ScreenUniverse:
    symbols: list[str]
    filters: ScreenFilters

    def all_symbols(self, config: Config, journal=None) -> list[str]:
        """Core ∪ promotions ∪ screen pool − demotions."""
        from .learning import effective_core_symbols

        seen: set[str] = set()
        out: list[str] = []
        base = effective_core_symbols(config, journal) if journal is not None \
            else [s.upper() for s in config.settings.universe.core]
        for sym in base + self.symbols:
            s = sym.upper()
            if s in seen:
                continue
            seen.add(s)
            out.append(s)
        return out


def screen_universe_path() -> Path:
    return PROJECT_ROOT / "config" / "screen_universe.yaml"


def load_screen_universe(path: Path | None = None) -> ScreenUniverse:
    p = path or screen_universe_path()
    if not p.exists():
        return ScreenUniverse(symbols=[], filters=ScreenFilters())
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    filt = raw.get("filters") or {}
    symbols = [str(s).upper() for s in (raw.get("symbols") or [])]
    return ScreenUniverse(
        symbols=symbols,
        filters=ScreenFilters(
            min_price=float(filt.get("min_price", 5.0)),
            min_avg_daily_volume=int(filt.get("min_avg_daily_volume", 1_000_000)),
            max_spread_bps=float(filt.get("max_spread_bps", 40.0)),
            top_n=int(filt.get("top_n", 8)),
            ttl_hours=int(filt.get("ttl_hours", 36)),
            wake_score=float(filt.get("wake_score", 55.0)),
        ),
    )

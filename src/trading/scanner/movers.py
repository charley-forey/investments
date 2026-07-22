"""Deterministic movers job: rank screen universe, write candidates.json + triggers."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ..analytics.opportunity import Opportunity, scan_symbol, utcnow_iso
from ..config import Config
from ..triggers import Trigger, load_triggers, save_triggers
from .universe import load_screen_universe

log = logging.getLogger("trading.scanner.movers")


@dataclass
class MoversReport:
    screened: int = 0
    candidates: int = 0
    path: str = ""


def candidates_path(config: Config) -> Path:
    root = Path(config.settings.paths.journal_db).resolve().parent
    return root / "candidates.json"


def load_candidates(config: Config) -> list[dict]:
    path = candidates_path(config)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    now = datetime.now(timezone.utc)
    out = []
    for row in data.get("candidates") or []:
        exp = row.get("expires_at")
        if exp:
            try:
                if datetime.fromisoformat(exp) < now:
                    continue
            except ValueError:
                pass
        out.append(row)
    return out


def active_candidate_symbols(config: Config) -> set[str]:
    return {str(c["symbol"]).upper() for c in load_candidates(config) if c.get("symbol")}


def candidate_context(config: Config, *, limit: int = 8) -> str:
    """Compact markdown for strategy / premarket extra_context."""
    rows = load_candidates(config)[:limit]
    if not rows:
        return ""
    lines = ["Active scanner candidates (deterministic OpportunityScore):"]
    for r in rows:
        tmpl = r.get("template") or "?"
        lines.append(
            f"- {r['symbol']} score={r.get('score', 0):.0f} "
            f"day={100*float(r.get('day_pct') or 0):+.1f}% "
            f"rvol={float(r.get('rvol') or 0):.1f}x template={tmpl} "
            f"Trigger: {r['symbol']} {r.get('trigger_direction', 'near')} "
            f"{r.get('trigger_level', r.get('last', 0))}"
        )
    return "\n".join(lines)


def _spy_day_pct(broker) -> float:
    try:
        df = broker.get_bars("SPY", days=5)
        if df is None or len(df) < 2:
            return 0.0
        closes = [float(r["close"]) for _, r in df.iterrows()]
        return (closes[-1] - closes[-2]) / closes[-2] if closes[-2] else 0.0
    except Exception:
        return 0.0


def _news_count(store, symbol: str) -> int:
    if store is None:
        return 0
    try:
        since = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
        row = store.conn.execute(
            "SELECT COUNT(*) AS n FROM news_items WHERE symbol=? AND ts >= ?",
            (symbol.upper(), since),
        ).fetchone()
        return int(row["n"] if row else 0)
    except Exception:
        return 0


def _social_mentions(store, symbol: str) -> int:
    if store is None:
        return 0
    try:
        since = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
        row = store.conn.execute(
            "SELECT COUNT(*) AS n FROM social_posts WHERE symbol=? AND ts >= ?",
            (symbol.upper(), since),
        ).fetchone()
        return int(row["n"] if row else 0)
    except Exception:
        return 0


def _days_to_event(calendar_events: list[dict], symbol: str) -> int | None:
    """Nearest upcoming event days for symbol; None if unknown."""
    today = datetime.now(timezone.utc).date()
    best = None
    for ev in calendar_events:
        if str(ev.get("symbol", "")).upper() != symbol.upper():
            continue
        raw = ev.get("date") or ev.get("ts") or ""
        try:
            d = datetime.fromisoformat(str(raw)[:10]).date()
        except ValueError:
            continue
        delta = (d - today).days
        if delta < 0:
            continue
        if best is None or delta < best:
            best = delta
    return best


def _load_calendar(config: Config) -> list[dict]:
    path = Path(config.settings.paths.calendar_file)
    if not path.exists():
        # paths may be relative
        from ..config import PROJECT_ROOT
        path = PROJECT_ROOT / "data" / "calendar.json"
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return data
        return data.get("events") or data.get("items") or []
    except (OSError, json.JSONDecodeError):
        return []


def _load_weights(journal) -> dict[str, float] | None:
    raw = journal.get_state("opportunity_weights") if journal else None
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def run_movers_scan(config: Config, broker, journal=None, store=None) -> MoversReport:
    """Screen the liquid pool, score, persist top-N candidates, merge triggers."""
    import os

    from ..data.intel import IntelStore

    screen = load_screen_universe()
    filt = screen.filters
    symbols = screen.all_symbols(config, journal)
    spy_day = _spy_day_pct(broker)
    cal = _load_calendar(config)
    weights = _load_weights(journal)

    if store is None:
        intel_path = config.settings.paths.intel_db
        if os.path.exists(intel_path):
            store = IntelStore(intel_path)

    scored: list[Opportunity] = []
    for sym in symbols:
        opp = scan_symbol(
            broker, sym, spy_day_pct=spy_day,
            news_count_24h=_news_count(store, sym),
            days_to_event=_days_to_event(cal, sym),
            social_mentions=_social_mentions(store, sym),
            min_price=max(filt.min_price, config.limits.symbols.min_price),
            min_adv=float(max(filt.min_avg_daily_volume,
                              config.limits.symbols.min_avg_daily_volume)),
            max_spread_bps=filt.max_spread_bps,
            bars_lookback=config.settings.agents.bars_lookback_days,
            weights=weights,
        )
        if opp is not None:
            # Tag core vs pure scanner for metrics.
            if sym in {s.upper() for s in config.settings.universe.core}:
                opp.discovery_source = "core"
            scored.append(opp)

    scored.sort(key=lambda o: o.score, reverse=True)
    top = scored[: filt.top_n]
    expires = (datetime.now(timezone.utc) + timedelta(hours=filt.ttl_hours)).isoformat(
        timespec="seconds"
    )
    payload = {
        "updated_at": utcnow_iso(),
        "wake_score": filt.wake_score,
        "candidates": [
            {**o.as_dict(), "expires_at": expires, "score_at_entry": o.score}
            for o in top
        ],
    }
    path = candidates_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    # Merge scanner triggers with existing watchlist triggers.
    keep = [t for t in load_triggers(config)
            if not str(getattr(t, "raw", "")).startswith("scanner:")]
    merged = {(t.symbol, t.direction, t.level): t for t in keep}
    for o in top:
        t = Trigger(
            symbol=o.symbol, direction=o.trigger_direction, level=o.trigger_level,
            raw=f"scanner:{o.template}:{o.score}",
        )
        merged[(t.symbol, t.direction, t.level)] = t
    save_triggers(config, list(merged.values()), source="watchlist+scanner")

    if journal is not None:
        journal.heartbeat(
            "movers", status="ok",
            detail=f"screened={len(symbols)} candidates={len(top)} "
                   f"top={[c.symbol for c in top[:5]]}",
        )

    log.info("movers scan: screened=%d candidates=%d -> %s",
             len(symbols), len(top), path)
    return MoversReport(screened=len(symbols), candidates=len(top), path=str(path))

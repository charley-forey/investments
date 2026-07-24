"""Deterministic intraday trigger gate.

Parses structured `Trigger: SYMBOL above|below|near LEVEL` lines from the
premarket watchlist into `data/triggers.json`, then decides whether an
intraday cycle should invoke the (expensive) strategy LLM.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from .config import Config
from .data.journal import Journal

_TRIGGER_RE = re.compile(
    r"Trigger:\s*([A-Z]{1,6})\s+(above|below|near)\s+([0-9]+(?:\.[0-9]+)?)",
    re.IGNORECASE,
)
_NEAR_PCT = 0.005  # 0.5% band for "near"


@dataclass
class Trigger:
    symbol: str
    direction: str  # above | below | near
    level: float
    raw: str = ""


def triggers_path(config: Config) -> Path:
    root = Path(config.settings.paths.journal_db).resolve().parent
    return root / "triggers.json"


def parse_triggers(text: str) -> list[Trigger]:
    out: list[Trigger] = []
    seen: set[tuple[str, str, float]] = set()
    for m in _TRIGGER_RE.finditer(text or ""):
        symbol = m.group(1).upper()
        direction = m.group(2).lower()
        level = float(m.group(3))
        key = (symbol, direction, level)
        if key in seen:
            continue
        seen.add(key)
        out.append(Trigger(symbol=symbol, direction=direction, level=level, raw=m.group(0)))
    return out


def save_triggers(config: Config, triggers: list[Trigger], *, source: str = "") -> Path:
    path = triggers_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "source": source,
        "triggers": [asdict(t) for t in triggers],
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def load_triggers(config: Config) -> list[Trigger]:
    path = triggers_path(config)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    out = []
    for row in data.get("triggers") or []:
        try:
            out.append(Trigger(
                symbol=str(row["symbol"]).upper(),
                direction=str(row["direction"]).lower(),
                level=float(row["level"]),
                raw=str(row.get("raw") or ""),
            ))
        except (KeyError, TypeError, ValueError):
            continue
    return out


def extract_and_save_from_watchlist(config: Config, watchlist_text: str) -> list[Trigger]:
    triggers = parse_triggers(watchlist_text)
    save_triggers(config, triggers, source="watchlist.md")
    return triggers


def _et_now() -> datetime:
    return datetime.now(ZoneInfo("America/New_York"))


def _forced_scan_slot(now_et: datetime | None = None) -> bool:
    """Always run a full LLM scan at open / midday / close for situational awareness."""
    now = now_et or _et_now()
    hm = now.hour * 60 + now.minute
    windows = [
        (9 * 60 + 30, 10 * 60 + 15),   # open
        (12 * 60 + 0, 12 * 60 + 45),   # midday
        (15 * 60 + 0, 15 * 60 + 45),   # close
    ]
    return any(lo <= hm <= hi for lo, hi in windows)


def _trigger_hit(broker, trigger: Trigger, *, tolerance_pct: float = _NEAR_PCT) -> bool:
    try:
        q = broker.get_quote(trigger.symbol)
        px = float(q.mid or 0)
    except Exception:
        return False
    if px <= 0 or trigger.level <= 0:
        return False
    if trigger.direction == "above":
        return px >= trigger.level
    if trigger.direction == "below":
        return px <= trigger.level
    # near
    return abs(px - trigger.level) / trigger.level <= tolerance_pct


def _position_needs_attention(account, broker, config: Config) -> bool:
    """Wake the agent if any open position is near its software stop/target band."""
    if not account.positions:
        return False
    stop_pct = abs(getattr(config.limits.exits, "stop_loss_pct", 8.0) or 8.0)
    target_pct = abs(getattr(config.limits.exits, "take_profit_pct", 25.0) or 25.0)
    # Wake when within 25% of the stop/target distance (getting close).
    near_stop = stop_pct * 0.75
    near_target = target_pct * 0.75
    for p in account.positions:
        entry = float(getattr(p, "avg_entry_price", 0) or 0)
        if entry <= 0:
            continue
        try:
            px = float(broker.get_quote(p.symbol).mid or 0)
        except Exception:
            continue
        if px <= 0:
            continue
        pl_pct = (px - entry) / entry * 100.0
        if float(p.qty) < 0:
            pl_pct = -pl_pct
        if pl_pct <= -near_stop or pl_pct >= near_target:
            return True
    return False


def _regime_shift(journal: Journal, broker, *, threshold_pct: float = 0.4) -> bool:
    """Compare SPY mid to the last recorded mark; wake on a meaningful move."""
    try:
        spy = float(broker.get_quote("SPY").mid or 0)
    except Exception:
        return False
    if spy <= 0:
        return False
    prev = journal.get_state("trigger_spy_mid")
    journal.set_state("trigger_spy_mid", f"{spy:.4f}")
    if not prev:
        return False
    try:
        prev_px = float(prev)
    except ValueError:
        return False
    if prev_px <= 0:
        return False
    return abs(spy - prev_px) / prev_px * 100.0 >= threshold_pct


@dataclass
class GateDecision:
    run_llm: bool
    reason: str


def should_run_intraday_llm(config: Config, journal: Journal, broker, account,
                            *, now_et: datetime | None = None) -> GateDecision:
    """Cheap pure-Python gate. When False, skip the strategy LLM this cycle."""
    agents = config.settings.agents
    if not getattr(agents, "trigger_gate_enabled", True):
        return GateDecision(True, "trigger gate disabled")

    if _forced_scan_slot(now_et):
        return GateDecision(True, "forced situational-awareness scan slot")

    if account.positions and _position_needs_attention(account, broker, config):
        return GateDecision(True, "open position near stop/target")

    if _regime_shift(journal, broker):
        return GateDecision(True, "regime shift (SPY move)")

    # High OpportunityScore candidates wake the LLM without waiting for a price print.
    try:
        from .analytics.autocalibrate import effective_wake_score
        from .scanner.movers import load_candidates
        wake = effective_wake_score(config, journal)
        hot = [c for c in load_candidates(config)
               if float(c.get("score") or 0) >= wake]
        if hot:
            syms = ", ".join(f"{c['symbol']}={c['score']:.0f}" for c in hot[:4])
            return GateDecision(True, f"opportunity score >= {wake:g}: {syms}")
    except Exception:
        pass

    hits = []
    for t in load_triggers(config):
        if _trigger_hit(broker, t):
            hits.append(f"{t.symbol} {t.direction} {t.level:g}")
    if hits:
        return GateDecision(True, "trigger hit: " + "; ".join(hits[:4]))

    return GateDecision(False, "no trigger / no forced slot — skipping LLM")

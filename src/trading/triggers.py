"""Deterministic intraday trigger gate.

Parses structured `Trigger: SYMBOL above|below|near LEVEL` lines from the
premarket watchlist into `data/triggers.json`, then decides whether an
intraday cycle should invoke the (expensive) strategy LLM.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from .config import Config
from .data.journal import Journal

_TRIGGER_RE = re.compile(
    r"Trigger:\s*([A-Z]{1,6})\s+(above|below|near)\s+([0-9]+(?:\.[0-9]+)?)",
    re.IGNORECASE,
)
_NEAR_PCT = 0.005  # 0.5% band for "near"
# A candidate already known at this score is not news; a jump of this many points is.
_SCORE_JUMP = 10.0
_SEEN_CANDIDATES_KEY = "gate_seen_candidates"

# Wake-event cooldown, keyed on (symbol, kind).
#
# The tick stream debounces per symbol at 30 minutes, but the universe is ~90 names,
# so a single risk-off open queues hundreds of events: 2026-07-28 recorded 475 (29 in
# the minute 13:35 alone). pending_wake_events() drains at most 20 per cycle and the
# poller runs every minute, so the backlog alone guaranteed ~24 back-to-back LLM
# sessions — 72 that day, $12.83, one proposal. Every other gate below already got a
# dedup for exactly this reason; this path never did.
#
# An event still wakes the LLM the first time. A repeat of the SAME (symbol, kind)
# inside the window is suppressed unless price has moved materially since — an ORB
# that keeps extending is genuinely new information, one that sits there is not.
_WAKE_COOLDOWN_KEY = "gate_wake_cooldown"
_WAKE_COOLDOWN_MINUTES = 20.0
_WAKE_REPRICE_PCT = 1.0


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


_FORCED_WINDOWS = [
    ("open", 9 * 60 + 30, 10 * 60 + 15),
    ("midday", 12 * 60 + 0, 12 * 60 + 45),
    ("close", 15 * 60 + 0, 15 * 60 + 45),
]


def _forced_scan_slot(now_et: datetime | None = None) -> str | None:
    """The situational-awareness window we're inside, or None."""
    now = now_et or _et_now()
    hm = now.hour * 60 + now.minute
    for name, lo, hi in _FORCED_WINDOWS:
        if lo <= hm <= hi:
            return name
    return None


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


def _novel_wake_events(journal: Journal, events: list[dict],
                       *, now: datetime | None = None) -> list[dict]:
    """Drop wake events whose (symbol, kind) already woke the LLM recently.

    State is one JSON blob under a single kv key -- same shape as
    _SEEN_CANDIDATES_KEY -- so a busy open cannot write hundreds of kv rows.
    """
    now = now or datetime.now(timezone.utc)
    try:
        seen = json.loads(journal.get_state(_WAKE_COOLDOWN_KEY) or "{}")
    except ValueError:
        seen = {}

    fresh: list[dict] = []
    for e in events:
        key = f"{e.get('symbol')}:{e.get('kind')}"
        price = e.get("price")
        prior = seen.get(key)
        if isinstance(prior, list) and len(prior) == 2:
            prior_ts, prior_px = prior
            try:
                age_min = (now - datetime.fromisoformat(prior_ts)).total_seconds() / 60.0
            except (TypeError, ValueError):
                age_min = _WAKE_COOLDOWN_MINUTES + 1
            moved = (
                price is not None and prior_px
                and abs(float(price) - float(prior_px)) / abs(float(prior_px)) * 100.0
                >= _WAKE_REPRICE_PCT
            )
            if age_min < _WAKE_COOLDOWN_MINUTES and not moved:
                continue
        fresh.append(e)
        seen[key] = [now.isoformat(), price]

    # Forget entries older than one cooldown so the blob cannot grow without bound.
    cutoff = now - timedelta(minutes=_WAKE_COOLDOWN_MINUTES)
    for key in list(seen):
        entry = seen[key]
        try:
            if datetime.fromisoformat(entry[0]) < cutoff:
                del seen[key]
        except (TypeError, ValueError, IndexError):
            del seen[key]

    journal.set_state(_WAKE_COOLDOWN_KEY, json.dumps(seen))
    return fresh


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

    # Events the tick stream saw between cycles. These are the whole point of the
    # stream: a level crossed at 10:02 and faded by 10:12 used to be invisible to a
    # 15-minute cron. Drained here so the same event cannot bill twice.
    # Drain the whole burst, not 20 at a time: a 475-event open took 24 cycles to
    # clear at the old limit, and each of those cycles billed a session. Novelty is
    # judged across the entire batch below, so a big drain is now the cheap path.
    pending = journal.pending_wake_events(limit=500)
    if pending:
        journal.consume_wake_events([int(e["id"]) for e in pending])
        fresh = _novel_wake_events(journal, pending)
        if fresh:
            detail = "; ".join(f"{e['symbol']} {e['kind']} {e['detail'] or ''}".strip()
                               for e in fresh[:4])
            return GateDecision(True, f"market event: {detail}")

    # Situational awareness means ONE look per window, not one per cycle inside it.
    # These three 45-minute windows cover 135 of the session's 390 minutes, so
    # re-firing every cycle spent ~$2.30/day re-reading the same tape.
    window = _forced_scan_slot(now_et)
    if window:
        today = (now_et or _et_now()).date().isoformat()
        seen_key = f"forced_slot:{today}:{window}"
        if journal.get_state(seen_key) is None:
            journal.set_state(seen_key, "1")
            return GateDecision(True, f"situational-awareness scan ({window})")

    if account.positions and _position_needs_attention(account, broker, config):
        return GateDecision(True, "open position near stop/target")

    if _regime_shift(journal, broker):
        return GateDecision(True, "regime shift (SPY move)")

    # A high OpportunityScore wakes the LLM — but only when it is NEWS.
    #
    # candidates.json is sorted by score and truncated to top_n (8 of 88), with a
    # 36h TTL, so "is anything above the wake score?" was always yes: the best of 88
    # names clears a middling bar by construction. That made this gate structurally
    # unable to say no — it fired on 26 of 26 cycles on 2026-07-27 and cost $5.97 to
    # rediscover that NVDA still scores 100. Wake on a NEW name or a materially
    # rising one; a score that has been 100 all afternoon is not an event.
    try:
        from .analytics.autocalibrate import effective_wake_score
        from .scanner.movers import load_candidates
        wake = effective_wake_score(config, journal)
        hot = {c["symbol"]: float(c.get("score") or 0) for c in load_candidates(config)
               if float(c.get("score") or 0) >= wake}
        prev = json.loads(journal.get_state(_SEEN_CANDIDATES_KEY) or "{}")
        fresh = {s: v for s, v in hot.items()
                 if s not in prev or v - prev[s] >= _SCORE_JUMP}
        if hot != prev:
            journal.set_state(_SEEN_CANDIDATES_KEY, json.dumps(hot))
        if fresh:
            syms = ", ".join(f"{s}={v:.0f}"
                             + ("" if s not in prev else f" (+{v - prev[s]:.0f})")
                             for s, v in list(fresh.items())[:4])
            return GateDecision(True, f"new/rising opportunity >= {wake:g}: {syms}")
    except Exception:
        pass

    hits = []
    for t in load_triggers(config):
        if _trigger_hit(broker, t):
            hits.append(f"{t.symbol} {t.direction} {t.level:g}")
    if hits:
        return GateDecision(True, "trigger hit: " + "; ".join(hits[:4]))

    return GateDecision(False, "no trigger / no forced slot — skipping LLM")

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

# Minimum gap between LLM sessions on the ROUTINE wake path.
#
# The per-(symbol, kind) cooldown above throttles each name, but 41 watched names
# each waking once per window still bought 146 sessions/day: any novel event in
# any minute purchased a whole session. Lengthening the per-symbol cooldown barely
# helps (90 min still gives 85 sessions) because the events are spread across
# symbols, not repeated on one. A global floor is the lever that actually bites:
# replayed against 2026-07-29's real 547 events, 5 minutes gives 55 sessions
# against that day's 146-equivalent.
#
# This throttles ONLY ordinary market-event wakes. Explicit agent-named triggers,
# a position near its stop/target, regime shifts and the forced situational-
# awareness windows all bypass it, exits never reach this gate at all (they run
# deterministically upstream), and armed plans fire from the tick stream without
# an LLM. Worst case is a few minutes' delay reacting to an ORB on a watched name,
# and the agent re-reads live quotes when it does run.
_LAST_LLM_TS_KEY = "gate_last_llm_ts"
_MIN_SESSION_GAP_MINUTES = 5.0


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


def _actionable_symbols(config: Config, journal: Journal, account) -> set[str]:
    """Symbols the agent could actually do something about this cycle.

    The tick stream watches ~88 names and emits an ORB event for any of them: 508
    on 2026-07-29, 475 the day before. But the agent only ever reasons about names
    it holds, has armed, has on the watchlist, or the scanner surfaced -- the
    reasoning log for all 124 sessions that day shows exactly that.

    The core universe is included deliberately, even though it widens the set: the
    scanner only refreshes candidates every 15 minutes, so gating purely on
    watchlist+candidates would blind us to a genuine breakout on a core name for up
    to a quarter of an hour. Dropping ~57 non-core streamed names is most of the
    saving and costs no coverage the agent was using.
    """
    syms: set[str] = set()
    try:
        syms |= {s.upper() for s in (config.settings.universe.core or [])}
    except Exception:
        pass
    try:
        syms |= {p.symbol.upper() for p in (account.positions or [])}
    except Exception:
        pass
    for loader in (
        lambda: {p["symbol"].upper() for p in journal.active_armed_plans()},
        lambda: {t.symbol.upper() for t in load_triggers(config)},
    ):
        try:
            syms |= loader()
        except Exception:
            pass
    try:
        from .scanner.movers import load_candidates

        syms |= {str(c.get("symbol", "")).upper() for c in load_candidates(config)}
    except Exception:
        pass
    return {s for s in syms if s}


def _armed_symbols(journal: Journal) -> set[str]:
    try:
        return {p["symbol"].upper() for p in journal.active_armed_plans()}
    except Exception:
        return set()


def _actionable_wake_events(config: Config, journal: Journal, account,
                            events: list[dict]) -> list[dict]:
    """Drop wake events the LLM cannot act on, before they cost a session.

    Two filters, in order of how much they save:

    * ARMED. If a symbol already carries an armed plan, the decision is made and
      the tick stream executes it in milliseconds without us. Waking to re-reach
      the same conclusion is the exact loop that ran four times a minute on
      2026-07-29 ("NET is at 266 vs its 277 trigger") at ~$0.12 a look.
    * UNWATCHED. An `orb` event on a name that is not held, armed, on the
      watchlist, or a live candidate has nothing behind it. Explicit `trigger`
      events are never dropped -- those are levels the agent named itself.
    """
    armed = _armed_symbols(journal)
    watched = _actionable_symbols(config, journal, account)
    out = []
    for e in events:
        sym = str(e.get("symbol") or "").upper()
        if sym in armed:
            continue
        if e.get("kind") == "orb" and sym not in watched:
            continue
        out.append(e)
    return out


def _minutes_since_last_session(journal: Journal, now: datetime) -> float:
    raw = journal.get_state(_LAST_LLM_TS_KEY)
    if not raw:
        return float("inf")
    try:
        return (now - datetime.fromisoformat(raw)).total_seconds() / 60.0
    except (TypeError, ValueError):
        return float("inf")


@dataclass
class GateDecision:
    run_llm: bool
    reason: str


def should_run_intraday_llm(config: Config, journal: Journal, broker, account,
                            *, now_et: datetime | None = None) -> GateDecision:
    """Cheap pure-Python gate. When False, skip the strategy LLM this cycle.

    Thin wrapper so the "when did we last spend money" clock is stamped in exactly
    one place, whichever of the seven reasons below fired. This is the last check
    before the strategy agent runs, so a True here means a session.
    """
    decision = _gate_decision(config, journal, broker, account, now_et=now_et)
    if decision.run_llm:
        journal.set_state(_LAST_LLM_TS_KEY, datetime.now(timezone.utc).isoformat())
    return decision


def _gate_decision(config: Config, journal: Journal, broker, account,
                   *, now_et: datetime | None = None) -> GateDecision:
    agents = config.settings.agents
    if not getattr(agents, "trigger_gate_enabled", True):
        return GateDecision(True, "trigger gate disabled")

    # Events the tick stream saw between cycles. These are the whole point of the
    # stream: a level crossed at 10:02 and faded by 10:12 used to be invisible to a
    # 15-minute cron. Drained here so the same event cannot bill twice.
    # Drain the whole burst, not 20 at a time: a 475-event open took 24 cycles to
    # clear at the old limit, and each of those cycles billed a session. Novelty is
    # judged across the entire batch below, so a big drain is now the cheap path.
    throttled = None
    pending = journal.pending_wake_events(limit=500)
    if pending:
        journal.consume_wake_events([int(e["id"]) for e in pending])
        # Actionability first, then novelty: no point spending cooldown state on
        # events that could never have justified a session.
        pending = _actionable_wake_events(config, journal, account, pending)
        fresh = _novel_wake_events(journal, pending)
        if fresh:
            gap = _minutes_since_last_session(journal, datetime.now(timezone.utc))
            if gap >= _MIN_SESSION_GAP_MINUTES:
                detail = "; ".join(f"{e['symbol']} {e['kind']} {e['detail'] or ''}".strip()
                                   for e in fresh[:4])
                return GateDecision(True, f"market event: {detail}")
            # Throttled, NOT returned: every gate below still gets its say, so a
            # named trigger, a position near its stop, or a regime shift can still
            # fire inside the window. Only the routine market-event path waits.
            throttled = (f"market event throttled ({gap:.1f}m since last session, "
                         f"min {_MIN_SESSION_GAP_MINUTES:g}m)")

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

    # A hit on a symbol that is already armed needs no LLM: the plan fires from the
    # tick stream, through the full guardrail pipeline, in milliseconds.
    armed = _armed_symbols(journal)
    hits = []
    for t in load_triggers(config):
        if t.symbol.upper() not in armed and _trigger_hit(broker, t):
            hits.append(f"{t.symbol} {t.direction} {t.level:g}")
    if hits:
        return GateDecision(True, "trigger hit: " + "; ".join(hits[:4]))

    return GateDecision(False, throttled or "no trigger / no forced slot — skipping LLM")

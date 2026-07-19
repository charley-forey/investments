"""Health/watchdog: derive liveness from the heartbeats table and alert if the
daemon has gone quiet. Also composes the daily "alive + what I did" summary."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .config import Config
from .data.journal import Journal
from . import notify


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    dt = datetime.fromisoformat(ts)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


@dataclass
class Health:
    healthy: bool
    last_heartbeat_ts: str | None
    last_success_ts: str | None
    minutes_since_heartbeat: float | None
    reason: str

    def summary(self) -> str:
        state = "HEALTHY" if self.healthy else "UNHEALTHY"
        age = ("n/a" if self.minutes_since_heartbeat is None
               else f"{self.minutes_since_heartbeat:.0f}m ago")
        return f"{state} — last heartbeat {age}; {self.reason}"


def check_health(journal: Journal, *, max_stale_minutes: float = 60.0,
                 now: datetime | None = None) -> Health:
    now = now or datetime.now(timezone.utc)
    last = journal.last_heartbeat()
    last_success = journal.last_successful_cycle()
    last_ts = last["ts"] if last else None
    dt = _parse(last_ts)

    if dt is None:
        return Health(False, None, None, None, "no heartbeats recorded yet")

    minutes = (now - dt).total_seconds() / 60.0
    if minutes > max_stale_minutes:
        return Health(False, last_ts, last_success["ts"] if last_success else None,
                      minutes, f"stale > {max_stale_minutes:.0f}m")

    # Look for recent error heartbeats.
    errors = [h for h in journal.recent_heartbeats(20) if h["status"] == "error"]
    if errors:
        return Health(True, last_ts, last_success["ts"] if last_success else None,
                      minutes, f"alive but {len(errors)} recent error(s)")

    return Health(True, last_ts, last_success["ts"] if last_success else None,
                  minutes, "ok")


def run_watchdog(config: Config, journal: Journal, *, max_stale_minutes: float = 60.0) -> Health:
    """Check health; alert via notify if unhealthy. Returns the Health result."""
    health = check_health(journal, max_stale_minutes=max_stale_minutes)
    journal.heartbeat("watchdog", status="ok" if health.healthy else "warn",
                      detail=health.summary())
    if not health.healthy:
        notify.send(config, f"⚠️ **Trading daemon watchdog**: {health.summary()}")
    return health


def daily_summary(config: Config, journal: Journal) -> str:
    """Compose (and send) a daily 'I'm alive + what I did' summary."""
    day_ago = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    cost = journal.cost_since(day_ago)
    health = check_health(journal)
    cycles = [h for h in journal.recent_heartbeats(100)
              if h["job"].startswith("cycle:") and h["status"] == "end"
              and (h["ts"] or "") >= day_ago]
    lines = [
        f"**Daily summary** — {health.summary()}",
        f"cycles completed (24h): {len(cycles)}",
        f"Anthropic cost (24h): ${cost:.2f}",
    ]
    pending = journal.pending_approvals()
    if pending:
        lines.append(f"⏳ {len(pending)} order(s) awaiting approval")
    msg = "\n".join(lines)
    notify.send(config, msg)
    journal.heartbeat("daily_summary", detail=f"cost ${cost:.2f}, {len(cycles)} cycles")
    return msg

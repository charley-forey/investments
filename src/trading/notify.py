"""Outbound notifications via Discord webhook. Uses stdlib urllib (no extra dep),
never raises into the caller, and no-ops cleanly when no webhook is configured."""

from __future__ import annotations

import json
import urllib.error
import urllib.request

from .config import Config


def send(config: Config, message: str, *, timeout: float = 10.0) -> bool:
    """Post a message to the configured Discord webhook. Returns True on success,
    False if unconfigured or on any failure (failures are non-fatal by design)."""
    url = config.secrets.discord_webhook_url
    if not url:
        return False
    # Discord hard-caps content at 2000 chars.
    content = message if len(message) <= 1900 else message[:1897] + "..."
    data = json.dumps({"content": content}).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return 200 <= resp.status < 300
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


def notify_cycle(config: Config, journal, report) -> None:
    """Fire a Discord summary for a completed cycle, if configured."""
    if not config.secrets.discord_webhook_url:
        return
    ok = send(config, f"**[{report.cycle}]** {report.summary()}")
    journal.heartbeat("notify", status="ok" if ok else "skip",
                      detail=f"cycle {report.cycle}")


def notify_fill(config: Config, journal, report) -> None:
    """Fire a Discord message when fills land, if configured."""
    if not config.secrets.discord_webhook_url:
        return
    send(config, f"💰 **{report.fills_recorded} fill(s)** synced "
                 f"(lots +{report.lots_opened}/-{report.lots_closed}, "
                 f"{report.day_trades_flagged} day trade(s))")


def notify_event(config: Config, journal, title: str, detail: str = "") -> None:
    """Fire a Discord alert for a notable event (kill switch, halt, error)."""
    if not config.secrets.discord_webhook_url:
        return
    ok = send(config, f"⚠️ **{title}**\n{detail}")
    journal.heartbeat("notify", status="ok" if ok else "skip", detail=title)

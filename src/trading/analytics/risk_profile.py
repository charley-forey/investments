"""The risk dial — one adjustable knob over the balanced baseline in limits.yaml.

Three named profiles scale *sizing* (risk/trade, gross exposure, options max loss,
trades/day) up or down. Everything that is a safety FLOOR — the daily-loss kill
switch, drawdown circuit, PDT, min DTE, defined-risk-only — is deliberately absent
here and never moves. More risk means bigger positions inside the same floor.

The active profile is a single JSON file (data/risk_profile.json), read at config
load time and overlaid onto the parsed limits. `aggressive` is gated behind the same
track-record eligibility as the live-scaling ladder (analytics/scaling.py); a human
may `--force` past the gate, and that override is journaled.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

# Profiles are field OVERRIDES on the balanced baseline (config/limits.yaml).
# "balanced" is the baseline itself, so it carries no overrides (identity overlay).
PROFILES: dict[str, dict[str, float]] = {
    "conservative": {
        "risk_per_trade_pct": 0.5,
        "max_gross_exposure_pct": 60.0,
        "max_new_trades_per_day": 5,
        "options_max_loss_usd": 500.0,
    },
    "balanced": {},
    "aggressive": {
        "risk_per_trade_pct": 2.0,
        "max_gross_exposure_pct": 150.0,
        "max_new_trades_per_day": 8,
        "options_max_loss_usd": 2000.0,
    },
}
DEFAULT_PROFILE = "balanced"
# Profiles that raise risk above balanced must be earned (or forced). Others are free.
GATED = {"aggressive"}


def apply_profile(limits, profile: str):
    """Return a copy of `limits` with the named profile's sizing overrides applied.
    Unknown/balanced profiles return the limits unchanged. Pure — no I/O."""
    over = PROFILES.get(profile)
    if not over:  # None (unknown) or {} (balanced) -> baseline unchanged
        return limits
    return limits.model_copy(update={
        "position": limits.position.model_copy(
            update={"risk_per_trade_pct": over["risk_per_trade_pct"]}),
        "portfolio": limits.portfolio.model_copy(
            update={"max_gross_exposure_pct": over["max_gross_exposure_pct"]}),
        "orders": limits.orders.model_copy(
            update={"max_new_trades_per_day": int(over["max_new_trades_per_day"])}),
        "options": limits.options.model_copy(
            update={"max_loss_per_trade_usd": over["options_max_loss_usd"]}),
    })


# -- active-profile persistence (single source of truth, offline-safe) --------

def _state_path(config) -> Path:
    # Sits next to candidates.json / triggers.json under the resolved journal dir.
    return Path(config.settings.paths.journal_db).parent / "risk_profile.json"


def read_active(config) -> str:
    """The active profile name, or the default. Never raises."""
    try:
        data = json.loads(_state_path(config).read_text(encoding="utf-8"))
        name = data.get("profile", DEFAULT_PROFILE)
        return name if name in PROFILES else DEFAULT_PROFILE
    except Exception:
        return DEFAULT_PROFILE


def write_active(config, profile: str, *, forced: bool, set_by: str = "cli") -> None:
    payload = {
        "profile": profile,
        "forced": forced,
        "set_by": set_by,
        "ts": datetime.now(timezone.utc).isoformat(),
    }
    _state_path(config).write_text(json.dumps(payload, indent=2), encoding="utf-8")


# -- eligibility (reuses the live-scaling track-record gate) ------------------

@dataclass
class Decision:
    profile: str
    eligible: bool
    reason: str


def check_eligibility(journal, rates, profile: str) -> Decision:
    """Whether the track record supports a gated profile. Ungated profiles are
    always eligible. Gated ones reuse scaling's level-1 gate (scored trades +
    a profitable strategy)."""
    if profile not in PROFILES:
        return Decision(profile, False, f"no such profile '{profile}'")
    if profile not in GATED:
        return Decision(profile, True, "no track record required")
    from . import scaling

    e = scaling.check_eligibility(journal, rates, 1)
    return Decision(profile, e.eligible, e.reason)


def set_profile(config, journal, rates, profile: str, *, force: bool = False,
                set_by: str = "cli") -> Decision:
    """Apply a profile if eligible (or forced). Returns the decision; only writes
    state when the change is allowed. A forced override is journaled."""
    if profile not in PROFILES:
        return Decision(profile, False, f"no such profile '{profile}'")
    decision = check_eligibility(journal, rates, profile)
    if decision.eligible or force:
        write_active(config, profile, forced=force and not decision.eligible, set_by=set_by)
        if force and not decision.eligible:
            journal.heartbeat(
                "risk_profile",
                detail=f"FORCED to '{profile}' despite ineligibility: {decision.reason}")
        else:
            journal.heartbeat("risk_profile", detail=f"set to '{profile}'")
        return Decision(profile, True, "forced override (unproven edge)"
                        if force and not decision.eligible else decision.reason)
    return decision


def effective_summary(limits) -> str:
    return (f"risk/trade {limits.position.risk_per_trade_pct:g}% | "
            f"gross {limits.portfolio.max_gross_exposure_pct:g}% | "
            f"options max loss ${limits.options.max_loss_per_trade_usd:,.0f} | "
            f"{limits.orders.max_new_trades_per_day} trades/day")

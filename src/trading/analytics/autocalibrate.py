"""Auto-calibration: the 'act' half of the learning loop.

`analytics/calibration.py` measures but only reports — a human/LLM reads it.
This closes the loop: nightly it turns the graded ledger (candidate_outcomes +
per-strategy stats) into *bounded* parameter changes and writes them to kv_state,
where the consumers read them clamped by the yaml.

Safety is structural, not incidental:
  - nothing is applied until the ledger has >= min_outcomes graded samples;
  - every parameter has a hard [lo, hi] clamp and a max step per run;
  - the adjustment is shrunk toward the current value when the sample is small
    (n < Bound.min_n), so early noise barely moves anything;
  - dry-run logs the proposed change without applying it (graduation window);
  - the kill switch, drawdown circuit, and defined-risk/notional guardrails are
    never calibrated — they are not in this module's reach.

Only tradeable-size and screening-sensitivity dials are auto-tuned. Lifecycle
promotion/demotion already runs deterministically via the scorer (run_lifecycle).
"""

from __future__ import annotations

from dataclasses import dataclass

from ..config import Config
from ..data.journal import Journal

# kv_state keys the consumers read (clamped by the yaml floor/ceiling).
WAKE_KEY = "cal_wake_score"
SIZE_KEY_PREFIX = "cal_size:"          # cal_size:<strategy_tag> -> [0,1] multiplier


@dataclass(frozen=True)
class Bound:
    lo: float
    hi: float
    max_step: float          # max absolute change per run
    min_n: int = 30          # full-strength adjustment needs this many samples


# Hard, non-config safe ranges. Deliberately in code, not yaml — these are the
# guardrails on the guardrail-tuner and should not be casually widened.
WAKE_BOUND = Bound(lo=40.0, hi=80.0, max_step=5.0, min_n=40)
SIZE_BOUND = Bound(lo=0.25, hi=1.0, max_step=0.25, min_n=20)


def bounded_adjust(current: float, proposed: float, bound: Bound, n: int) -> float:
    """Move `current` toward `proposed`, shrunk for small n, clamped to one
    max_step and to [lo, hi]. Pure — this is the safety kernel, tested directly."""
    shrink = min(1.0, n / bound.min_n) if bound.min_n > 0 else 1.0
    target = current + (proposed - current) * shrink
    step = max(-bound.max_step, min(bound.max_step, target - current))
    return round(_clamp(current + step, bound.lo, bound.hi), 4)


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


@dataclass
class Adjustment:
    param: str
    old: float
    new: float
    n: int
    rationale: str

    def line(self) -> str:
        return f"{self.param}: {self.old:g} -> {self.new:g} (n={self.n}, {self.rationale})"


# -- proposed values from the ledger -----------------------------------------

def propose_wake_score(journal: Journal, current: float) -> Adjustment | None:
    """Nudge the scanner wake score by whether high-scoring candidates actually
    preceded moves in their predicted direction. Good hit rate -> surface more
    (lower the bar); poor -> be pickier (raise it)."""
    row = journal.conn.execute(
        "SELECT COUNT(*) n, AVG(direction_right) hit FROM candidate_outcomes "
        "WHERE direction_right IS NOT NULL"
    ).fetchone()
    n = int(row["n"] or 0)
    if n == 0:
        return None
    hit = float(row["hit"] or 0.0)
    # Center on 0.5 (coin flip). Above -> the funnel has signal, widen it; below ->
    # tighten. Scale the nudge by how far from 0.5, up to the bound's max step.
    proposed = current - (hit - 0.5) * 2.0 * WAKE_BOUND.max_step * 2.0
    new = bounded_adjust(current, proposed, WAKE_BOUND, n)
    if new == round(current, 4):
        return None
    return Adjustment(WAKE_KEY, round(current, 4), new, n,
                      f"candidate hit-rate {hit:.0%}")


def propose_size_multipliers(journal: Journal, config: Config) -> list[Adjustment]:
    """Per-strategy sizing multiplier from fractional Kelly on realized stats.
    Only ever scales risk down (cap 1.0); a weak/again edge shrinks toward the
    floor so a paper strategy keeps generating sample without over-betting."""
    from ..guardrails.account_math import kelly_fraction
    from .stats import stats_by_tag

    out: list[Adjustment] = []
    by_tag = stats_by_tag(journal, config.settings.tax)
    for tag, st in by_tag.items():
        if st.trades <= 0 or st.avg_loss == 0:
            continue
        wl_ratio = abs(st.avg_win) / abs(st.avg_loss) if st.avg_loss else 0.0
        kelly = kelly_fraction(st.win_rate, wl_ratio, cap=0.25)
        # Map quarter-Kelly (0.25) -> full size (1.0); scale down proportionally.
        proposed = _clamp(kelly / 0.25, SIZE_BOUND.lo, SIZE_BOUND.hi)
        key = f"{SIZE_KEY_PREFIX}{tag}"
        current = float(journal.get_state(key, "1.0") or 1.0)
        new = bounded_adjust(current, proposed, SIZE_BOUND, st.trades)
        if new != round(current, 4):
            out.append(Adjustment(key, round(current, 4), new, st.trades,
                                  f"kelly {kelly:.2f} @ win {st.win_rate:.0%} R {wl_ratio:.2f}"))
    return out


# -- runner -------------------------------------------------------------------

def _total_graded(journal: Journal) -> int:
    c = journal.conn.execute("SELECT COUNT(*) FROM candidate_outcomes").fetchone()[0]
    p = journal.conn.execute(
        "SELECT COUNT(*) FROM proposal_outcomes WHERE hypothetical_pnl IS NOT NULL"
    ).fetchone()[0]
    return int(c) + int(p)


def run_autocalibrate(config: Config, journal: Journal) -> list[Adjustment]:
    """Compute and (unless dry-run/disabled/under-sampled) apply bounded changes.
    Returns the adjustments — applied or merely proposed — for logging."""
    agents = config.settings.agents
    if not agents.auto_calibrate_enabled:
        return []
    total = _total_graded(journal)
    if total < agents.auto_calibrate_min_outcomes:
        journal.heartbeat("autocalibrate", status="skip",
                          detail=f"{total} graded < min {agents.auto_calibrate_min_outcomes}")
        return []

    from ..scanner.universe import load_screen_universe
    current_wake = float(journal.get_state(WAKE_KEY, "") or
                         load_screen_universe().filters.wake_score)

    changes: list[Adjustment] = []
    w = propose_wake_score(journal, current_wake)
    if w:
        changes.append(w)
    changes.extend(propose_size_multipliers(journal, config))

    applied = not agents.auto_calibrate_dry_run
    if applied:
        for c in changes:
            journal.set_state(c.param, str(c.new))
    mode = "applied" if applied else "dry-run"
    for c in changes:
        journal.heartbeat("autocalibrate", status="ok", detail=f"[{mode}] {c.line()}")
    if changes:
        try:
            from .. import notify
            notify.notify_event(
                config, journal, f"Auto-calibration ({mode})",
                "\n".join(c.line() for c in changes))
        except Exception:
            pass
    return changes


def effective_wake_score(config: Config, journal: Journal) -> float:
    """Calibrated wake score if set (clamped to the safe range), else the yaml
    default. The single read the trigger gate uses."""
    from ..scanner.universe import load_screen_universe
    base = load_screen_universe().filters.wake_score
    raw = journal.get_state(WAKE_KEY)
    if raw:
        try:
            return _clamp(float(raw), WAKE_BOUND.lo, WAKE_BOUND.hi)
        except ValueError:
            pass
    return base


def size_multiplier(journal: Journal, strategy_tag: str) -> float:
    """Calibrated sizing multiplier for a strategy (clamped), default 1.0."""
    raw = journal.get_state(f"{SIZE_KEY_PREFIX}{strategy_tag}")
    if raw:
        try:
            return _clamp(float(raw), SIZE_BOUND.lo, SIZE_BOUND.hi)
        except ValueError:
            pass
    return 1.0

"""The strategy registry: one name per strategy, joined across every subsystem.

Before this existed there were three unrelated naming systems — the scanner's
template (`analytics/opportunity.suggest_template`), the playbook filename, and the
backtest signal (`cli.SIGNALS`) — and the intersection of playbooks and backtest
signals was the **empty set**. `strategy_tag` on a proposal was unvalidated free
text, so the agent invented tags (`trend-breakout-long`, `relative-strength-long`)
which then became permanent keys in `scores`, `kv_state` stages, and the
auto-calibration ledger. Nothing could be looked up by anything else:
`candidate_grading.regime_edge` matches a `strategy_tag` against the
`candidate_outcomes.template` column and was a permanent no-op for every invented
tag.

One dict fixes all of it. A tag in here can be scanned for, proposed, backtested,
graded, staged and sized — and a tag not in here cannot be proposed at all.

Deliberately a plain module-level dict of a handful of entries. Not YAML, not a
plugin system.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Strategy:
    tag: str
    signal: str | None      # function name in backtest.strategies; None = not backtestable
    playbook: str | None    # playbooks/<stem>.md — the agent's licence to trade it
    scanner: bool = False   # may suggest_template emit this tag?
    proposable: bool = True # may the agent name it on a proposal?

    @property
    def backtestable(self) -> bool:
        return self.signal is not None


STRATEGIES: dict[str, Strategy] = {
    # The only tag with validated edge: +0.459 mean OOS R over 1,280 trades, 9y,
    # 10 symbols (docs/backtest_trailing_exit_2026-07-28.md) and +$264.66 in the
    # shadow ledger. It had no playbook until now, which is most of why it was
    # rarely traded.
    "trend-pullback-long": Strategy(
        "trend-pullback-long", "trend_pullback_long", "trend-pullback-long",
        scanner=True),
    "momentum-continuation": Strategy(
        "momentum-continuation", "momentum_continuation", "momentum-continuation",
        scanner=True),
    # Renamed from "orb-breakout", which measured no opening range: the scanner rule
    # is distance from the 20d SMA times relative volume, on daily bars. A name that
    # lies is worse than no name.
    "extended-from-sma": Strategy(
        "extended-from-sma", "extended_from_sma", "extended-from-sma", scanner=True),
    "breakout": Strategy("breakout", "breakout", "breakout"),
    # Baseline for the sweep to measure the others against; not a licence to trade.
    "sma-crossover": Strategy(
        "sma-crossover", "sma_crossover", None, proposable=False),
}


def get(tag: str) -> Strategy | None:
    return STRATEGIES.get((tag or "").strip().lower())


def proposable_tags() -> list[str]:
    return sorted(t for t, s in STRATEGIES.items() if s.proposable)


def scanner_tags() -> list[str]:
    return sorted(t for t, s in STRATEGIES.items() if s.scanner)


def backtestable() -> list[Strategy]:
    """What the nightly sweep runs. Order is stable so its report is diffable."""
    return [STRATEGIES[t] for t in sorted(STRATEGIES) if STRATEGIES[t].backtestable]


def signal_for(tag: str):
    """Resolve a tag to a ready-to-run backtest signal, or None."""
    s = get(tag)
    if s is None or s.signal is None:
        return None
    from backtest import strategies as _sig

    fn = getattr(_sig, s.signal, None)
    return fn() if fn is not None else None


def validate_tag(tag: str, asset_class: str = "stock") -> str | None:
    """Error string if this tag may not be proposed, else None.

    Options are exempt: their tags are synthesized by `propose_vertical`
    (`debit-call-vertical` and friends) rather than chosen by the model, and the
    defined-risk guardrails govern them instead.
    """
    if asset_class != "stock":
        return None
    s = get(tag)
    if s is None or not s.proposable:
        return (f"unknown strategy_tag '{tag}'. Choose one of: "
                f"{', '.join(proposable_tags())}. Each has a playbook — read it "
                f"with read_playbook before proposing.")
    return None

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
    # The bearish side. Registering the long tags made every short tag
    # unregisterable (`relative-strength-short`, bearish `news-impulse`), so the
    # book went long-only the day that landed — while the regime sweep says these
    # signals pay in down/elevated, which a long-only book cannot express.
    # They start at `unproven` (0.25x) like everything else and must earn size
    # through the nightly sweep, not through assertion.
    "trend-pullback-short": Strategy(
        "trend-pullback-short", "trend_pullback_short", "trend-pullback-short"),
    "breakdown": Strategy("breakdown", "breakdown", "breakdown"),
    # Buys statistical stretch below the mean and waits for reversion. Targets the
    # cells where every trend rule bleeds -- up/calm and sideways/calm, which
    # together are most of the decade and currently have almost no positive
    # strategy at all.
    "mean-reversion": Strategy(
        "mean-reversion", "mean_reversion", "mean-reversion"),
    # The only entry here that is not a trend bet. All the others are long-vol
    # directional rules that want the same tape, which is exactly why they share a
    # regime signature and all fail in the same cells. This one is short-vol and
    # mean-reverting: it sells rich premium and profits from price NOT moving.
    #
    # signal=None on purpose. There is no options price history to replay, so it
    # cannot be backtested and can never pass the sweep gate. It therefore stays at
    # `unproven` (0.25x) and earns its record from the live grading ledger instead
    # -- which is the honest treatment, not a loophole: quarter size on an
    # unvalidated idea is the same deal every other tag gets.
    "vol-premium": Strategy("vol-premium", None, "vol-premium"),
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


# Tags `propose_vertical` synthesizes as "{mode}-{right}-vertical". They are not
# registry strategies -- there is no options price history to backtest them
# against -- but they are legitimate and must pass validation.
_SYNTHESIZED_OPTION_TAGS = frozenset(
    f"{mode}-{right}-vertical" for mode in ("debit", "credit")
    for right in ("call", "put")
)


def validate_tag(tag: str, asset_class: str = "stock") -> str | None:
    """Error string if this tag may not be proposed, else None.

    Options used to be exempt entirely, on the reasoning that `propose_vertical`
    synthesizes their tags rather than the model choosing them. But `propose_order`
    also accepts `asset_class="option"` with hand-built legs and a free-text tag,
    so the exemption was a hole: on 2026-07-30 proposal #37 was SUBMITTED under
    `relative-strength-long` -- a tag deleted for grading -$901 and rejected on
    every stock proposal. The registry existed to stop exactly that.

    Options now accept registry tags plus the synthesized vertical names, and
    reject anything else.
    """
    t = (tag or "").strip().lower()
    if asset_class != "stock":
        if t in _SYNTHESIZED_OPTION_TAGS or get(t) is not None:
            return None
        return (f"unknown strategy_tag '{tag}' for an option. Use a registered "
                f"strategy ({', '.join(proposable_tags())}) or let propose_vertical "
                f"synthesize the tag.")
    s = get(tag)
    if s is None or not s.proposable:
        return (f"unknown strategy_tag '{tag}'. Choose one of: "
                f"{', '.join(proposable_tags())}. Each has a playbook — read it "
                f"with read_playbook before proposing.")
    return None

"""Nightly walk-forward sweep: the backtest becomes the research engine.

Every real edge finding this project has produced came from the backtester, not from
the LLM — the reward:risk study (2026-07-27) and the trailing-exit study (2026-07-28).
Both were run by hand, once. Meanwhile the lifecycle ladder that decides which
strategies get capital (`candidate -> backtest -> paper`) was unreachable dead code,
because nothing ever ran a backtest against a live tag: `promote_after_backtest` and
`gate_strategy` had no caller and no data.

This closes that loop. Every strategy in the registry is replayed nightly over the
stored bar history, walk-forward across sequential folds and across the whole
universe, and the verdict feeds the stage that sizes its live positions.

Two deliberate asymmetries:

* **Promote only.** A pass can lift a strategy to `paper`, never beyond — automatic
  promotion cannot reach `small-live`/`scaled`, so the machine cannot widen its own
  mandate into real money. Demotion stays with `evaluate_tag`, driven by real closed
  trades, which is the evidence that should be able to take capital away.
* **Exits mirror live config.** The sweep replays with the trailing stop the live
  guardrails actually attach, so a pass means "this works the way we trade it" rather
  than "this works with some other exit policy".
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from ..config import Config
from ..data.journal import Journal

# Mechanical stop for the replay. Live stops are agent-chosen and 1-2%, but a fixed
# basis is what makes symbols and strategies comparable; it is the same 2% both prior
# studies used, so their numbers remain reproducible against this harness.
STOP_PCT = 0.02
MIN_TRADES_PER_SYMBOL = 20      # below this a per-symbol mean R is noise
MIN_SYMBOLS = 5                 # below this a cross-symbol verdict is noise
MIN_POSITIVE_FRACTION = 0.6     # share of symbols that must beat buy-and-hold
MIN_MEAN_ALPHA_R = 0.0


def _buy_hold_r(bars) -> float:
    """Total R a passive holder would have collected over this window.

    The gate has to be measured against SOMETHING. Absolute R is not it: the first
    run of this sweep passed all five strategies, and the highest score went to
    `sma-crossover`, the deliberately dumb baseline. Ten years of US large caps rise,
    a 2% stop turns that drift into a large R number, and any long-biased rule
    collects it. A gate that promotes everything, including the control, is measuring
    the market and not the strategy."""
    if len(bars) < 2 or bars[0].close <= 0:
        return 0.0
    return ((bars[-1].close - bars[0].close) / bars[0].close) / STOP_PCT


def _exposure(trades, n_bars: int) -> float:
    """Fraction of the window the strategy actually held a position.

    Raw buy-and-hold is the wrong hurdle on its own: it is long 100% of the time,
    while these signals are in the market roughly a third of it. Comparing the two
    totals asks "should I have just held?" (a portfolio question) rather than "is
    this signal better than being passively long?" (the question that should gate a
    strategy). Scaling the benchmark by exposure asks the second one."""
    if n_bars <= 0:
        return 0.0
    held = sum(max(1, t.exit_idx - t.entry_idx) for t in trades)
    return min(1.0, held / n_bars)


def _folds(bars, n_folds: int):
    """Same sequential split walk_forward uses, so the comparison lines up exactly."""
    size = len(bars) // n_folds
    if size < 2:
        return []
    return [bars[k * size:(len(bars) if k == n_folds - 1 else (k + 1) * size)]
            for k in range(n_folds)]


@dataclass
class TagResult:
    tag: str
    symbols_tested: int = 0
    symbols_positive: int = 0     # symbols where the strategy beat buy-and-hold
    trades: int = 0
    mean_r: float = 0.0           # mean OOS R per trade (absolute)
    mean_alpha_r: float = 0.0     # mean total R per fold ABOVE buy-and-hold
    # Alpha per fold index, averaged across symbols. Folds are sequential slices of
    # 2016-2026, so this exposes regime dependence: a rule that trails a passive hold
    # in the melt-up but beats it in the drawdown is doing something worth having,
    # and the single averaged number hides that completely.
    fold_alpha: list[float] = field(default_factory=list)
    passed: bool = False
    promoted_from: str | None = None
    note: str = ""

    @property
    def positive_fraction(self) -> float:
        return self.symbols_positive / self.symbols_tested if self.symbols_tested else 0.0

    def summary(self) -> str:
        if self.symbols_tested == 0:
            return f"{self.tag}: no data ({self.note})"
        verdict = "PASS" if self.passed else "fail"
        line = (f"{self.tag}: {verdict} — {self.mean_alpha_r:+.1f}R vs buy-and-hold, "
                f"{self.symbols_positive}/{self.symbols_tested} symbols beat it, "
                f"{self.mean_r:+.3f} R/trade, {self.trades} trades")
        if self.promoted_from:
            line += f" — PROMOTED {self.promoted_from} -> paper"
        return line


@dataclass
class SweepReport:
    results: list[TagResult] = field(default_factory=list)
    promoted: list[str] = field(default_factory=list)

    def as_markdown(self) -> str:
        lines = ["| strategy | verdict | R vs buy-and-hold | symbols beating it "
                 "| R/trade | trades |",
                 "|---|---|---|---|---|---|"]
        for r in sorted(self.results, key=lambda x: -x.mean_alpha_r):
            if r.symbols_tested == 0:
                lines.append(f"| {r.tag} | no data | — | — | — | — |")
                continue
            lines.append(
                f"| {r.tag} | {'**PASS**' if r.passed else 'fail'} "
                f"| {r.mean_alpha_r:+.1f} | {r.symbols_positive}/{r.symbols_tested} "
                f"| {r.mean_r:+.3f} | {r.trades} |")
        return "\n".join(lines)


def _universe(config: Config) -> list[str]:
    """The screen pool if configured, else the core universe."""
    try:
        from ..scanner.universe import load_screen_universe

        syms = load_screen_universe().symbols
        if syms:
            return syms
    except Exception:
        pass
    return list(config.settings.universe.core)


def run_sweep(config: Config, journal: Journal, *, symbols: list[str] | None = None,
              n_folds: int = 4, promote: bool = True) -> SweepReport:
    """Replay every backtestable strategy over stored bars and gate on the result."""
    from backtest.engine import Bar, run_backtest

    from .. import strategies as registry
    from ..analytics import lifecycle
    from ..data.bars import BarStore

    trail = config.limits.exits.trailing_pct
    bt_kwargs = {"stop_pct": STOP_PCT}
    if trail and trail > 0:
        # Mirror the live exit: a trailing stop and no resting target. Measured on
        # 2026-07-28 -- with a fixed target the trail never fires, so a sweep that
        # kept one would be validating an exit policy we do not run.
        bt_kwargs["trail_pct"] = float(trail) / 100.0

    store = BarStore(config.settings.paths.bars_db)
    report = SweepReport()
    try:
        universe = symbols or _universe(config)
        cache: dict[str, list] = {}
        for sym in universe:
            rows = store.load_bars(sym)
            if len(rows) >= 200:
                cache[sym] = [Bar(date=b.date, open=b.open, high=b.high, low=b.low,
                                  close=b.close, volume=b.volume) for b in rows]

        for strat in registry.backtestable():
            res = TagResult(tag=strat.tag)
            signal = registry.signal_for(strat.tag)
            if signal is None:
                res.note = f"signal '{strat.signal}' not found"
                report.results.append(res)
                continue

            per_symbol_r: list[float] = []
            per_symbol_alpha: list[float] = []
            by_fold: list[list[float]] = []
            for sym, bars in cache.items():
                # Run the folds directly rather than via walk_forward: the alpha
                # measure needs each fold's individual trades to compute exposure,
                # and FoldResult only carries aggregates.
                fold_rs: list[float] = []
                fold_alphas: list[float] = []
                sym_trades = 0
                for seg in _folds(bars, n_folds):
                    try:
                        bt = run_backtest(seg, signal, **bt_kwargs)
                    except Exception:
                        continue
                    rs = [t.r_multiple for t in bt.trades if t.r_multiple is not None]
                    if not rs:
                        continue
                    sym_trades += len(rs)
                    fold_rs.extend(rs)
                    # Beat being passively long for the same amount of exposure.
                    benchmark = _buy_hold_r(seg) * _exposure(bt.trades, len(seg))
                    fold_alphas.append(sum(rs) - benchmark)

                if sym_trades < MIN_TRADES_PER_SYMBOL or not fold_alphas:
                    continue
                alpha = sum(fold_alphas) / len(fold_alphas)

                for k, a in enumerate(fold_alphas):
                    while len(by_fold) <= k:
                        by_fold.append([])
                    by_fold[k].append(a)
                per_symbol_r.append(sum(fold_rs) / len(fold_rs))
                per_symbol_alpha.append(alpha)
                res.trades += sym_trades
                if alpha > 0:
                    res.symbols_positive += 1

            res.symbols_tested = len(per_symbol_alpha)
            if res.symbols_tested < MIN_SYMBOLS:
                res.note = (f"only {res.symbols_tested} symbols cleared "
                            f"{MIN_TRADES_PER_SYMBOL} trades (need {MIN_SYMBOLS})")
                report.results.append(res)
                _persist(journal, res)
                continue

            res.mean_r = sum(per_symbol_r) / len(per_symbol_r)
            res.mean_alpha_r = sum(per_symbol_alpha) / len(per_symbol_alpha)
            res.fold_alpha = [round(sum(f) / len(f), 2) for f in by_fold if f]
            res.passed = (strat.proposable
                          and res.mean_alpha_r > MIN_MEAN_ALPHA_R
                          and res.positive_fraction >= MIN_POSITIVE_FRACTION)

            if res.passed and promote:
                stage_before = lifecycle.get_stage(journal, strat.tag)
                change = lifecycle.promote_after_backtest(
                    journal, strat.tag, res.mean_alpha_r,
                    min_expectancy=MIN_MEAN_ALPHA_R)
                if change:
                    res.promoted_from = stage_before
                    report.promoted.append(strat.tag)
            report.results.append(res)
            _persist(journal, res)
    finally:
        store.close()

    journal.heartbeat(
        "backtest_sweep", status="ok",
        detail=f"{len(report.results)} strategies, {len(report.promoted)} promoted")
    return report


def _persist(journal: Journal, res: TagResult) -> None:
    """One kv row per tag. Deliberately not a new table -- nothing reads history yet."""
    journal.set_state(f"backtest:{res.tag}", json.dumps({
        "mean_r": round(res.mean_r, 4),
        "mean_alpha_r": round(res.mean_alpha_r, 3),
        "symbols_tested": res.symbols_tested,
        "symbols_positive": res.symbols_positive,
        "trades": res.trades,
        "passed": res.passed,
        "fold_alpha": res.fold_alpha,
    }))


def sweep_context(journal: Journal) -> str:
    """Compact standing of each strategy, for the strategy agent's prompt."""
    from .. import strategies as registry

    lines = []
    for strat in registry.backtestable():
        raw = journal.get_state(f"backtest:{strat.tag}")
        if not raw:
            continue
        try:
            d = json.loads(raw)
        except ValueError:
            continue
        lines.append(
            f"  {strat.tag}: {d.get('mean_alpha_r', 0):+.1f}R vs buy-and-hold over "
            f"{d['trades']} backtested trades, {d['symbols_positive']}/"
            f"{d['symbols_tested']} symbols beat it"
            f"{' [VALIDATED]' if d['passed'] else ''}")
    if not lines:
        return ""
    return "Backtest standing (nightly walk-forward):\n" + "\n".join(lines)

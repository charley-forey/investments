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


REGIME_MIN_TRADES = 40          # per (strategy, regime) before conditioning on it
REGIME_MIN_MULT = 0.25          # floor on the regime size multiplier
REGIME_FLOOR_R = 0.5            # alpha/trade at which the multiplier bottoms out
_REGIME_WINDOW = 60             # closes needed to classify (compute_regime needs 50)

# Below this alpha/trade the cell is not "worse", it is losing, and the right size
# is zero rather than a quarter. Shrink-only sizing left the catastrophic cells
# (sideways/elevated runs -1.05 to -2.29 R/trade across all five strategies) still
# trading at 0.25x, which is a smaller loss, not a gain. Measured over the stored
# sweep, gating instead of shrinking moves deployed alpha from ~+0.02 to ~+0.12
# per trade while still taking 41-55% of the trades.
#
# Deliberately well inside the noise band rather than at 0.0: a cell that merely
# fails to beat the benchmark is a sizing question, and only a clearly negative one
# is a skip. REGIME_MIN_TRADES still applies, so a thin cell is never gated.
REGIME_SKIP_BELOW_R = -0.05


def regime_key(trend: str | None, vol_state: str | None) -> str | None:
    if not trend or not vol_state or "unknown" in (trend, vol_state):
        return None
    return f"{trend}/{vol_state}"


def regime_labels(bars) -> list[str | None]:
    """Regime label per bar index, from the *same* classifier the live path uses.

    `compute_regime` is a pure function of a close series, so replaying it over
    history and calling it live cannot disagree — which is the whole point. Anything
    that re-implemented the thresholds would drift and the conditioning would be
    conditioning on a different regime than the one it was measured in.

    The first ~50 bars are None (insufficient history), and trades entered there are
    simply not attributed to any regime."""
    from ..tools.market_context import compute_regime

    closes = [b.close for b in bars]
    out: list[str | None] = []
    for i in range(len(closes)):
        if i < 49:
            out.append(None)
            continue
        window = closes[max(0, i - _REGIME_WINDOW + 1):i + 1]
        reg = compute_regime(window)
        out.append(regime_key(reg.trend, reg.vol_state))
    return out


def _accumulate_regime(acc: dict, seg, trades, labels_by_date: dict) -> None:
    """Split one fold's result into per-regime strategy R, benchmark R and exposure.

    A trade is attributed to the regime on its ENTRY bar — that is the regime the
    decision was made in, which is what the live gate will know at decision time.

    The benchmark has to be regime-matched too, or the comparison smuggles the whole
    period's drift into every bucket: buy-and-hold R inside a regime is the sum of
    daily returns on the bars carrying that label, and exposure is the share of those
    bars the strategy was actually holding.
    """
    for t in trades:
        if t.r_multiple is None or not (0 <= t.entry_idx < len(seg)):
            continue
        lab = labels_by_date.get(seg[t.entry_idx].date)
        if not lab:
            continue
        d = acc.setdefault(lab, {"r": 0.0, "trades": 0, "held": 0})
        d["r"] += t.r_multiple
        d["trades"] += 1
        d["held"] += max(1, t.exit_idx - t.entry_idx)

    for i in range(1, len(seg)):
        lab = labels_by_date.get(seg[i].date)
        if not lab or seg[i - 1].close <= 0:
            continue
        d = acc.setdefault(lab, {"r": 0.0, "trades": 0, "held": 0})
        d["bench_r"] = d.get("bench_r", 0.0) + (
            (seg[i].close - seg[i - 1].close) / seg[i - 1].close) / STOP_PCT
        d["bars"] = d.get("bars", 0) + 1


def _regime_alpha(acc: dict) -> dict:
    """Collapse the accumulator into {regime: {alpha, per_trade, trades}}.

    `alpha` is the total summed over every symbol and fold, so it scales with how
    much of history a regime occupied — useful for reading, useless for comparing.
    `per_trade` is what conditioning uses: alpha divided by the trades that earned
    it, in the same R units as everything else."""
    out = {}
    for lab, d in acc.items():
        bars = d.get("bars", 0)
        if not bars or d["trades"] == 0:
            continue
        exposure = min(1.0, d["held"] / bars)
        alpha = d["r"] - d.get("bench_r", 0.0) * exposure
        out[lab] = {"alpha": round(alpha, 2),
                    "per_trade": round(alpha / d["trades"], 4),
                    "trades": d["trades"]}
    return out


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
    # regime key -> {"alpha": R above an exposure-matched passive hold in that
    # regime, "trades": n}. This is the payload regime conditioning runs on.
    regime: dict = field(default_factory=dict)
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

    def regime_markdown(self) -> str:
        """Alpha per trade by regime — the table live sizing is conditioned on."""
        keys = sorted({k for r in self.results for k in r.regime})
        if not keys:
            return ""
        lines = ["| strategy | " + " | ".join(keys) + " |",
                 "|" + "---|" * (len(keys) + 1)]
        for r in sorted(self.results, key=lambda x: -x.mean_alpha_r):
            cells = []
            for k in keys:
                rec = r.regime.get(k)
                cells.append(f"{rec['per_trade']:+.2f} ({rec['trades']})" if rec else "—")
            lines.append(f"| {r.tag} | " + " | ".join(cells) + " |")
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

        # Regime is a property of the broad tape, not of each name, so it is read
        # once off SPY and applied to every symbol by date. Per-symbol labelling
        # would measure "is THIS stock trending", which is the signal itself and
        # would make the conditioning circular.
        labels_by_date: dict[str, str] = {}
        spy = cache.get("SPY") or cache.get("VOO")
        if spy:
            for bar, lab in zip(spy, regime_labels(spy)):
                if lab:
                    labels_by_date[bar.date] = lab

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
            regime_acc: dict = {}
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
                    if labels_by_date:
                        _accumulate_regime(regime_acc, seg, bt.trades, labels_by_date)

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
            res.regime = _regime_alpha(regime_acc)
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
        "regime": res.regime,
    }))


def _regime_record(journal: Journal, tag: str, regime: str | None) -> dict | None:
    """What the sweep measured for this strategy in this regime, if it is enough."""
    if not regime:
        return None
    raw = journal.get_state(f"backtest:{tag}")
    if not raw:
        return None
    try:
        rec = (json.loads(raw).get("regime") or {}).get(regime)
    except ValueError:
        return None
    if not rec or rec.get("trades", 0) < REGIME_MIN_TRADES:
        return None
    return rec


def regime_size_multiplier(journal: Journal, tag: str,
                           trend: str | None, vol_state: str | None) -> float:
    """<=1.0 sizing factor for trading `tag` in the current regime.

    The sweep's headline finding is that these strategies lose to a passive hold in
    strong uptrends and earn their keep in choppier tape — the effect is regime, not
    quality. Sizing is the right lever for that, not a veto: alpha being negative in
    a regime means "this is a worse use of risk here", not "this cannot work". A
    hard veto keyed on the same data would stop the system trading altogether
    whenever SPY trends, which is most of the time.

    Positive measured alpha -> full size. Negative -> scaled down toward
    REGIME_MIN_MULT in proportion to how negative, so the dial moves with the
    evidence instead of flipping. Unknown regime or too few trades -> 1.0, because
    absence of evidence must not quietly shrink every position.
    """
    rec = _regime_record(journal, tag, regime_key(trend, vol_state))
    if rec is None:
        return 1.0
    per_trade = float(rec.get("per_trade", 0.0))
    if per_trade >= 0:
        return 1.0
    # Clearly negative and well sampled -> do not trade it. Sizing a losing cell
    # down to 0.25x still loses; it just takes longer.
    if per_trade < REGIME_SKIP_BELOW_R:
        return 0.0
    # Per trade, not the total: the total scales with how much of the last decade a
    # regime happened to occupy, so `up/calm` would dominate purely by being common.
    # -0.5R per trade or worse bottoms out the multiplier; linear in between.
    return max(REGIME_MIN_MULT,
               1.0 + (per_trade / REGIME_FLOOR_R) * (1.0 - REGIME_MIN_MULT))


def regime_context(journal: Journal, trend: str | None, vol_state: str | None) -> str:
    """Per-strategy standing in the CURRENT regime, for the strategy prompt."""
    from .. import strategies as registry

    key = regime_key(trend, vol_state)
    if not key:
        return ""
    lines = []
    for strat in registry.backtestable():
        if not strat.proposable:
            continue
        rec = _regime_record(journal, strat.tag, key)
        if rec is None:
            continue
        mult = regime_size_multiplier(journal, strat.tag, trend, vol_state)
        lines.append(f"  {strat.tag}: {rec.get('per_trade', 0):+.3f}R/trade vs passive "
                     f"over {rec['trades']} backtested trades in this regime"
                     + (f" — sized x{mult:.2f}" if mult < 1.0 else " — full size"))
    if not lines:
        return ""
    return (f"Backtested edge in the CURRENT regime ({key}), measured over 10 years:\n"
            + "\n".join(lines))


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

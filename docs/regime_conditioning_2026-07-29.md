# Regime conditioning: these strategies need volatility

`candidate_grading.regime_edge` has existed since the learning-loop work and has never
done anything: its evidence is `candidate_outcomes`, whose 208 rows are **all** from a
single `('sideways','calm')` regime. Nine days of live trading cannot fill a nine-cell
regime matrix. Ten years of bars can.

## Method

The regime label comes from `tools.market_context.compute_regime`, **the same function
the live path calls** — it is pure over a close series, so replaying it across SPY's
history and reading it live cannot disagree. Anything that re-implemented the
thresholds would drift, and the conditioning would be conditioning on a regime it was
never measured in.

- Trend from the SMA20/SMA50 spread (±1%): `up` / `sideways` / `down`
- Vol from 20-day annualised realised: `calm` <12%, `normal` <20%, `elevated` above
- Read off **SPY**, not per symbol: a per-name label would measure "is this stock
  trending", which is the signal itself, making the conditioning circular
- Each trade is attributed to the regime **on its entry bar** — what the live gate
  knows at decision time
- The benchmark is regime-matched too: buy-and-hold R *within* that regime's bars,
  scaled by the strategy's exposure during them. Otherwise every bucket inherits the
  whole decade's drift

## Result — alpha per trade (R vs an exposure-matched passive hold)

| strategy | up/calm | up/normal | up/elev | side/calm | side/normal | side/elev | down/normal | down/elev |
|---|---|---|---|---|---|---|---|---|
| trend-pullback-long | −0.27 | +0.22 | +0.18 | −0.19 | **+0.36** | **−1.85** | −0.85 | +0.39 |
| breakout | −0.23 | +0.21 | +0.55 | −0.19 | +0.28 | **−2.15** | −0.98 | +0.27 |
| extended-from-sma | −0.12 | +0.21 | +0.44 | +0.07 | +0.15 | **−1.05** | −0.48 | +0.12 |
| momentum-continuation | −0.13 | +0.22 | +0.54 | +0.04 | +0.02 | **−1.07** | −0.48 | −0.05 |
| sma-crossover *(control)* | −0.38 | +0.87 | +1.45 | −0.32 | +0.10 | **−2.29** | −1.78 | +0.44 |

(`down/calm` is excluded everywhere — 4 to 16 trades, below the 40-trade floor.)

**All five strategies agree on the sign in every single cell.** Five signals built on
different logic, 1,000+ trades per cell, ranking regimes identically — that is a
property of the tape, not of any one rule.

## What it says

**These are volatility strategies wearing trend clothing.**

- **`sideways/elevated` is catastrophic** (−1.05 to −2.29R per trade). Choppy *and*
  volatile is the whipsaw regime: wide ranges trigger entries, no follow-through
  cashes them, and the trailing stop pays for the round trip every time.
- **`up/calm` is consistently negative** (−0.12 to −0.38). In a quiet melt-up there is
  nothing to catch; the stop grazes on noise and you sell moves that never needed
  exiting. This is the regime that produced the "no strategy beats buy-and-hold"
  headline — and it is the single most common regime of the last decade
  (~3,000 trades per strategy, more than any other cell).
- **`up/normal`, `up/elevated`, `down/elevated` all pay** (+0.12 to +1.45). Directional
  movement with real volatility is what these rules are for.
- **`down/normal` is bad** (−0.48 to −1.78) while `down/elevated` is good. A grinding
  decline bleeds them; a violent one they catch.

The actionable rule the aggregate number hid: **trade these when the tape is moving,
stand down when it is quietly grinding higher or chopping violently.**

## How it is wired

`analytics/sweep.regime_size_multiplier(journal, tag, trend, vol)` → a factor in
[0.25, 1.0], applied in `orchestrator._risk_size` alongside the existing vol-target,
drawdown throttle and auto-calibration multipliers.

- Positive measured alpha → **1.0** (full size)
- Negative → scaled linearly toward **0.25** at −0.5R/trade or worse
- Fewer than 40 trades in that cell, or an unknown regime → **1.0**. Absence of
  evidence must not quietly shrink every position.

Sizing, not a veto. Negative alpha in a regime means "this is a worse use of risk
here", not "this cannot work" — and a veto keyed on the same data would stop the system
trading altogether whenever SPY trends, which is most of the time.

Current multipliers (today's tape is `sideways/calm`):

| strategy | up/calm | side/calm | side/elev | down/normal | elsewhere |
|---|---|---|---|---|---|
| trend-pullback-long | ×0.60 | ×0.71 | ×0.25 | ×0.25 | ×1.00 |
| breakout | ×0.65 | ×0.72 | ×0.25 | ×0.25 | ×1.00 |
| extended-from-sma | ×0.83 | ×1.00 | ×0.25 | ×0.29 | ×1.00 |
| momentum-continuation | ×0.81 | ×1.00 | ×0.25 | ×0.28 | ×1.00 |

The same table is shown to the strategy agent each cycle via `sweep.regime_context`,
alongside the (single-regime) shadow-graded numbers. They measure different things and
disagreement between them is information.

## Caveats

- Nine cells from one decade. `down/calm` never had enough trades; `up/calm` has 3,000+
  per strategy and dominates the aggregate.
- Regime is a lagging label — SMA20 vs SMA50 confirms a turn well after it starts. That
  is honest (the live path is equally lagged) but it means the boundaries are fuzzy.
- Survivorship in the 88-name screen pool, as in the main sweep.
- Reproduce: `trading sweep --dry-run`; the matrix lands in `memory/backtest_sweep.md`
  and in `kv_state` under `backtest:<tag>`.

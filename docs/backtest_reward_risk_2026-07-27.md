# Where to set `min_reward_risk` — 1,279 trades instead of 20

`limits.orders.min_reward_risk` was set to **1.5** from 20 hand-graded
counterfactuals: median proposed R was 0.48 at a 45% win rate, and break-even at
that win rate is 1.22. That reasoning was sound but the sample was tiny.

With Algo Trader Plus we can now replay 9 years. Run:

```
trading backtest AAPL --strategy trend-pullback-long --days 3285 \
    --stop-pct 0.02 --target-r 3.0
```

## Setup

`trend_pullback_long` (the best template in the shadow ledger: +$264.66 on 4 wins
in 5 — five samples, hence this exercise). Daily bars, 2,655 per symbol, 10
symbols: AAPL MSFT NVDA XOM JPM SPY UNH AMD COST WMT. Fixed 2% stop, 100 shares,
production friction model. **1,279 trades.**

## Result

| target_r | n | win % | net $ | R/trade | stop/target/other |
|---|---|---|---|---|---|
| 1.0 | 1279 | 47.0% | +25,951 | +0.042 | 335/595/349 |
| **1.5** | 1279 | 39.6% | +50,648 | **+0.100** | 379/486/414 |
| 2.0 | 1279 | 35.0% | +67,329 | +0.142 | 410/405/464 |
| 2.5 | 1279 | 31.9% | +80,560 | +0.167 | 434/342/503 |
| 3.0 | 1279 | 30.6% | +105,340 | +0.210 | 445/299/535 |
| 4.0 | 1279 | 29.1% | +129,720 | +0.253 | 457/220/602 |
| 5.0 | 1279 | 28.6% | +167,499 | +0.319 | 462/180/637 |

Per-symbol at 3.0R — **every one positive**, so this is not one lucky name:

```
AAPL +0.672R   MSFT +0.069R   NVDA +0.308R   XOM  +0.109R   JPM +0.246R
SPY  +0.268R   UNH  +0.066R   AMD  +0.265R   COST +0.184R   WMT +0.109R
```

## What it means

Expectancy per trade rises monotonically with the target while the win rate
falls. **Cutting winners short is what costs us**, and the instinct to raise the
win rate by taking profit early is exactly backwards — 1.0R wins 47% of the time
and earns a quarter of what 3.0R earns.

## Walk-forward — the out-of-sample check

In-sample over 9 years is a lot of data but it is still one fit. Each symbol split
into **4 sequential out-of-sample folds** (~663 bars, ~2.0y each), 10 symbols =
**40 fold-observations**. `passed()` requires positive mean expectancy *and* ≥60%
of traded folds positive.

| target_r | positive folds | mean OOS R/trade | symbols passing gate |
|---|---|---|---|
| 1.5 | 24/40 (60.0%) | +0.119 | 5/10 |
| 2.0 | 30/40 (75.0%) | +0.163 | 8/10 |
| **2.5** | 30/40 (75.0%) | **+0.195** | 8/10 |
| 3.0 | 34/40 (85.0%) | +0.230 | 9/10 |

Per-symbol at 2.0 (`+`/`-` per fold): AAPL `+ + - +` PASS · MSFT `+ - + -` fail ·
NVDA `+ - + +` PASS · XOM `- - + +` fail · JPM `+ + + +` PASS · SPY `+ + + -` PASS
· UNH `+ - + +` PASS · AMD `+ + + -` PASS · COST `+ - + +` PASS · WMT `+ + + +` PASS

**The relationship survives out of sample, and gets stronger.** Robustness rises
with the target as well as expectancy — which the in-sample table could not show.

Note what this says about the originally shipped 1.5: only **5 of 10** symbols
passed, and folds were 60.0% positive — sitting exactly on the pass threshold.
That was marginal, not safe.

## Honest limits

- At high `target_r` the target stops being the exit mechanism: at 5.0R only 180
  of 1,279 trades reach it, and 637 exit on the signal. So the tail of this table
  measures "let the trend run" as much as "set a wide target". Read 2.0-3.0 as
  the supported range, not 5.0.
- Daily bars, long only, one template, fixed 2% stop. The live system takes
  intraday entries with agent-chosen 1-2% stops. Suggestive for the live config,
  not conclusive.
- One template. `momentum_continuation` and the credit structures are untested.
- Walk-forward folds are sequential, not re-fit — the signal is parameter-free, so
  each fold is a clean OOS test, but this does not simulate parameter drift.

## Decision

Raised `min_reward_risk` **1.5 -> 2.5**. Out of sample that nearly doubles mean R
per trade (+0.119 -> +0.195) and takes positive folds from 60% to 75%.

**Not 3.0, which tested better on both axes.** The backtest applies this ratio to
a mechanical 2% stop on daily bars. Live stops are agent-chosen and typically
1-2% on intraday entries, where R=3 means demanding a 3.6% move from a name whose
ATR is 2.5% — a multi-day move asked of an intraday trade. The backtest validates
the *direction* strongly and the *level* only loosely, because the stop basis
differs. Revisit once live fills exist and the two can be compared on like terms.

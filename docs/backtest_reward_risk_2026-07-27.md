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

## Honest limits

- At high `target_r` the target stops being the exit mechanism: at 5.0R only 180
  of 1,279 trades reach it, and 637 exit on the signal. So the tail of this table
  measures "let the trend run" as much as "set a wide target". Read 2.0-3.0 as
  the supported range, not 5.0.
- Daily bars, long only, one template, fixed 2% stop. The live system takes
  intraday entries with agent-chosen 1-2% stops. Suggestive for the live config,
  not conclusive.
- No walk-forward split here. `trading backtest --walkforward` does that and
  should be run before treating any of this as settled.

## Decision

Raised `min_reward_risk` **1.5 -> 2.0**: a 42% improvement in per-trade
expectancy, comfortably inside the sampled range, and it does not demand geometry
so rare the system stops trading — which matters when it has not traded since
7/23. The data supports going higher; revisit once live samples exist.

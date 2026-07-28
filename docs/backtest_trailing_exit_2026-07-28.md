# Let winners run: trailing stop vs a fixed R target

`limits.orders.min_reward_risk` was raised to 2.5 on 7/27 because expectancy rose
monotonically with the profit target across 1,279 trades. That fixed the *entry*
bar. The exit side still capped every winner at exactly its target: the guardrail
attached a resting take-profit at the proposal's `target_price`, so CRM on 7/28 was
going to be sold at 188 no matter how far it ran.

`exits.trailing_pct` existed for this, but was `null` and would have done nothing if
set — `ExitRules.high_water` is documented as caller-owned and nothing ever populated
it ("off until a high-water source is wired").

## Setup

Identical to `backtest_reward_risk_2026-07-27.md` so the numbers are comparable:
`trend_pullback_long`, daily bars 2016-01-04 → 2026-07-28, 2,656 bars per symbol,
10 symbols (AAPL MSFT NVDA XOM JPM SPY UNH AMD COST WMT), fixed 2% stop, 100 shares,
production friction model. **1,280 trades.**

`run_backtest` gained a `trail_pct` argument. It measures give-back from the peak
favorable price **as of the previous bar** — never the current bar's own high, which
would be exiting on a price the position had not yet seen. Same pessimism convention
as the existing stop-before-target ordering.

Reproduce:

```
trading backtest AAPL --strategy trend-pullback-long --days 3285 \
    --stop-pct 0.02 --trail-pct 0.08
```

## Result — full history

| exit rule | n | win % | net $ | R/trade |
|---|---|---|---|---|
| target 2.5R (previous) | 1280 | 31.9% | +80,517 | +0.167 |
| target 3.0R | 1280 | 30.6% | +105,297 | +0.210 |
| trail 4% | 1280 | 31.0% | +171,111 | +0.324 |
| trail 6% | 1280 | 29.1% | +208,057 | +0.394 |
| **trail 8%** | 1280 | 28.0% | +199,069 | **+0.417** |
| trail 10% | 1280 | 27.7% | +188,424 | +0.378 |
| trail 12% | 1280 | 27.6% | +190,282 | +0.413 |
| trail 8% **+** target 2.5R | 1280 | 31.9% | +80,517 | +0.167 |

The first two rows reproduce the 7/27 study exactly, which is the harness check.

**The last row is the important one.** Adding a trail to a fixed target changes
nothing at all — identical trades, identical P&L. The target is always nearer than
the trail, so it always fires first. You cannot have both; enabling `trailing_pct`
only means anything if the take-profit leg goes away. Hence the change in
`guardrails/engine.py`: when `trailing_pct` is set, no take-profit is attached.

Win rate *falls* (31.9% → 28.0%) while expectancy rises 2.5x. That is the whole
thesis — the tail pays for the losers.

## Out of sample — 4 sequential folds x 10 symbols = 40 windows

| exit rule | mean OOS R | positive folds | worst fold |
|---|---|---|---|
| target 2.5R (previous) | +0.197 | 29/40 (72%) | −0.325 |
| trail 6% | +0.404 | 34/40 (85%) | −0.386 |
| **trail 8%** | **+0.459** | 32/40 (80%) | **−0.279** |

Per-symbol at trail 8% vs target 2.5R, 8 of 10 improve; UNH (+0.043 vs +0.107) and
nothing else degrades. AMD +0.168 → +0.991 and NVDA +0.296 → +0.954 are the big
movers, but SPY (+0.227 → +0.260) improves too, so this is not one lucky high-beta
name.

## Adopted: `trailing_pct: 8`

8% over 6% on the two robustness metrics (mean OOS R, worst fold); 6% wins only on
positive-fold count, and by one fold. A wider trail also churns less — 87 trail exits
vs 162 — which matters live where friction is real and the backtest's is modelled.

The caveat that blocked `target_r: 3.0` on 7/27 — the backtest assumes a mechanical
2% stop while live stops are agent-chosen and 1-2% — **does not apply here.**
`target_r` is denominated in stop distances, so a narrower live stop moves the target.
`trail_pct` is a give-back off the peak price and is independent of stop width.

`exits.take_profit_pct` was also set to `null` (was 25). The measured arm is "trail,
no target"; leaving a +25% cap in place would reimpose the ceiling the trail exists
to remove, just further out.

## What did not change

The protective **stop** leg still rests at the broker as GTC, on every entry. Dropping
the take-profit means a dead daemon leaves a position with downside capped and upside
open — the right way round, and why `broker/sync.ensure_protective_stops` re-arms
stops and never targets.

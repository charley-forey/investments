# First full sweep: no strategy beats being passively long

The nightly sweep replays every registered strategy over the whole screen universe.
First run: **88 symbols, 197,814 daily bars (2016-01 → 2026-07), 40,212 backtested
trades**, 4 sequential walk-forward folds per symbol, 2% stop, 8% trailing exit
(mirroring live config), production friction.

## The result

| strategy | R vs buy-and-hold | symbols beating it | R/trade | trades |
|---|---|---|---|---|
| sma-crossover *(control)* | −0.7 | 37/87 | +0.536 | 3,632 |
| extended-from-sma | −1.7 | 33/88 | +0.271 | 9,088 |
| momentum-continuation | −1.8 | 33/88 | +0.265 | 8,614 |
| breakout | −2.0 | 33/88 | +0.389 | 10,084 |
| trend-pullback-long | −2.5 | 29/88 | +0.402 | 8,794 |

**Nothing passes.** Every strategy has positive expectancy per trade net of friction —
they make money — but none of them beats simply being long the same name for the same
amount of market exposure. Fewer than half the symbols beat the benchmark for any
strategy. Nothing was promoted; everything stays at `unproven` (25% sizing).

## How the gate got here (two wrong answers first)

**Attempt 1 — absolute R.** All five passed, and the highest score went to
`sma-crossover`, the deliberately dumb control. A 2% stop over a decade of large-cap
drift turns any long-biased rule into a big R number. That gate was measuring the
market, not the strategy.

**Attempt 2 — raw buy-and-hold.** Everything lost by 33–38R. Also wrong, in the other
direction: buy-and-hold is invested 100% of the time while these signals are in the
market roughly a third of it, so this asked "should I have just held?" — a portfolio
question — rather than "is this signal better than being passively long?"

**Adopted — exposure-scaled buy-and-hold.** The benchmark is buy-and-hold R times the
fraction of the window the strategy actually held a position. Same question, fair
units. Margins land at −0.7 to −2.5R, i.e. genuinely close rather than absurd.

That progression is why `MIN_MEAN_ALPHA_R` is not a knob to tune until something
passes. A gate nothing can fail is worth nothing.

## The finding that is actually useful: it is regime, not quality

Alpha per fold, oldest → newest (each fold ≈ 2.6 years):

| strategy | 2016–18 | 2018–21 | 2021–23 | 2023–26 |
|---|---|---|---|---|
| sma-crossover | −3.78 | **+1.11** | −0.39 | **+0.44** |
| trend-pullback-long | −8.36 | −2.28 | **+0.51** | **+0.12** |
| breakout | −6.74 | −2.90 | **+1.65** | −0.21 |
| extended-from-sma | −3.83 | −4.03 | **+0.58** | **+0.16** |
| momentum-continuation | −4.00 | −3.31 | −0.06 | **+0.03** |

Every strategy improves monotonically. All of them are heavily negative in the
2016–2018 melt-up and positive or flat in the choppier recent folds. That is the
signature of trend-following with stops: in a relentless uptrend, stops take you out
of moves that never needed exiting, and you pay for protection you did not use. In
choppier tape the protection is worth something.

**So these signals are not worthless — they are regime-dependent, and the regime we
tested is mostly the one they are worst in.** The actionable version of "no strategy
beats buy-and-hold" is: *be passively long in a confirmed uptrend, and deploy these
signals when the tape is not trending.* That is precisely what
`candidate_grading.regime_edge` was built to express and has never had data for — 208
graded rows, all of them from a single `('sideways','calm')` regime. Ten years of bars
can fill that matrix; nine days of live trading never will.

## Caveats

- Long-only survivorship: the 88-name screen pool is today's list, so names that
  failed out of the index are missing. This biases *toward* buy-and-hold, making the
  benchmark harder than reality — the strategies are treated conservatively, which is
  the right direction for a gate.
- Benchmark R is frictionless (one entry, one exit); strategy R is net of spread and
  slippage. Also conservative.
- Daily bars only. `extended-from-sma` and `momentum-continuation` are daily rules, so
  that is honest for them; anything genuinely intraday cannot be evaluated here at all.
- `sma-crossover` is the control and is marked non-proposable, so it can never be
  promoted regardless of score.

## Reproduce

```
trading sweep --dry-run          # report only, no stage changes
trading ingest --screen --days 3650   # refresh the bar history first
```

Runs nightly at 17:35 ET after the 17:30 bar ingest. Results land in
`memory/backtest_sweep.md` (read by the premarket agent), in `kv_state` under
`backtest:<tag>`, and in the strategy agent's prompt context.

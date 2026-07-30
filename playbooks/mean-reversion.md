# Mean reversion

**Unproven.** Scored +0.037 R/trade out of sample on the walk-forward gate, which
is positive but short of the 60% breadth requirement (42/88 symbols). It trades at
0.25x until a sweep says otherwise.

## Why this one exists

Every trend rule in the registry bleeds in quiet tape: a 2% stop grazes on noise
and sells moves that never needed exiting. This is the opposite bet — it buys
statistical stretch **below** the mean and waits for price to come back, so it
should earn where they lose.

It is long-only by construction. Shorting stretch in a market with upward drift is
fighting the drift, and the short tags already measure −0.53 in `up/calm`.

## Setup

- Close at or below **1.5 standard deviations** under its 20-day SMA
- Exit when price reverts to the mean (z >= 0), not on a fixed target
- Prefer liquid names: this is a high-turnover pattern and spread costs compound

## Where it actually pays

| regime | alpha/trade |
|---|---|
| up/normal | **+0.11** |
| sideways/normal | −0.02 |
| sideways/calm | −0.06 |
| up/calm | −0.08 |
| down/normal | −0.16 |
| down/elevated | −0.16 |
| sideways/elevated | **−0.60** |

The regime gate enforces this — it is available in `up/normal` and
`sideways/normal` and blocked elsewhere. Do not argue with it.

## Risk

- A stretch can always stretch further. The stop is not optional and the position
  is not averaged down — "it's even cheaper now" is how this pattern kills accounts
- Reward:risk at least 2.5, measured to the mean, not beyond it
- **Never mean-revert into a binary event.** A gap through the mean is not a
  stretch, it is a repricing, and the reversion premise is gone
- Check the reason for the move first: a stretch on news is a new fair value, a
  stretch on nothing is the trade

## Invalidation

- Close below the entry stop — the mean stopped holding
- The 20-day SMA itself rolls over: mean reversion needs a mean, and a falling
  mean is a downtrend wearing a stretch
- Volatility state turns elevated — this pattern measures −0.60 in
  `sideways/elevated`, the worst cell for it by a factor of ten

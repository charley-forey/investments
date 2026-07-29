# Breakout

**Unproven.** No backtest has been run on this signal yet, so it trades at reduced
size until the nightly sweep says otherwise. Check `memory/backtest_sweep.md`.

Not emitted by the scanner — propose it only when you have a specific level thesis
that the deterministic templates did not surface.

Setup:
- Close at a new 20-day closing high; flat on a new 20-day low
- A *held* break, not the first touch — the level has to become support
- Prefer a tight book; breakouts are where spread costs bite hardest

Risk:
- Stop back inside the range. If the break fails, the premise is gone immediately
- Reward:risk at least 2.5
- Do not chase more than ~1 ATR past the level without a pullback hold

Invalidation:
- Close back inside the prior range
- A break on declining volume that immediately reverses — that is a liquidity sweep,
  not a breakout

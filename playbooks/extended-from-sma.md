# Extended from SMA

**Unproven.** No backtest has been run on this signal yet, so it trades at reduced
size until the nightly sweep says otherwise. Check `memory/backtest_sweep.md` for its
current standing before sizing up.

Renamed from `orb-breakout`, which was a lie: that branch never looked at an opening
range. The scanner rule is distance from the 20d SMA scaled by relative volume, on
**daily** bars. The name now matches the measurement.

Use when OpportunityScore flags `extended-from-sma`.

Setup:
- Price stretched ~3.6%+ above its 20d SMA on relative volume >= 1.2
- Continuation only — this is a "strength persists" bet, not a breakout of a level
- Prefer names where the extension is recent, not a month-long grind

Risk:
- Stop back at or just below the 20d SMA; the premise is that price stays extended
- Mean reversion is the failure mode and it is fast — do not widen the stop
- Reward:risk at least 2.5

Invalidation:
- Close back below the 20d SMA
- Extension without volume: the rule needs both, and volume is the half that decays first

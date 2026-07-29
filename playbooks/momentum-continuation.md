# Momentum continuation

**Unproven.** No backtest has been run on this signal yet, so it trades at reduced
size until the nightly sweep says otherwise. Check `memory/backtest_sweep.md`.

Use when OpportunityScore flags `momentum-continuation`.

Setup:
- 20-day return beyond +/-5%, with a day move >= 2.5% on relative volume >= 1.5
- Two-sided: strong momentum long, weak momentum short
- The bet is that a move already underway keeps going, so demand the volume — a move
  on no volume is noise, not participation

Risk:
- Stop at the point that says the move stalled, not at an arbitrary percentage
- Reward:risk at least 2.5
- Shorts: borrow and squeeze risk are real, and the event-wall guardrail applies the
  same either way

Invalidation:
- 20-day return crosses back inside the threshold band — the premise has decayed
- Any gap through the stop: take it, do not average

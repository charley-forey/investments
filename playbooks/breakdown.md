# Breakdown

**Unproven.** No backtest has validated this signal yet, so it trades at reduced
size until the nightly sweep says otherwise. Check `memory/backtest_sweep.md`.

The bearish mirror of `breakout`. Not emitted by the scanner — propose it only when
you have a specific level thesis that the deterministic templates did not surface.

Setup:
- Close at a new 20-day closing low; flat on a new 20-day high
- A *held* break, not the first touch — the level has to become resistance
- Prefer a tight book and genuine size on the offer; a thin tape gaps against a short

Risk:
- Stop back inside the range. If the break fails, the premise is gone immediately
- Reward:risk at least 2.5
- **A short's loss is unbounded and gaps do not respect stops.** Size smaller than
  the equivalent long, and never hold one through the name's own earnings
- If the thesis is a dated catalyst, prefer `propose_vertical(direction="bearish")`
  — a put spread caps the loss at the debit and is exempt from the event wall

Invalidation:
- Close back inside the prior range
- A break on declining volume that immediately reverses — that is a liquidity sweep,
  not a breakdown
- Broad-market strength against the position: shorting a single name into a rising
  tape is fighting the drift that the sweep says beats these signals outright

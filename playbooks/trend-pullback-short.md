# Trend pullback (short)

**Unproven.** The bearish mirror of `trend-pullback-long`. The long side is the one
tag with validated edge (+0.459 mean OOS R, 1,280 trades); that says nothing about
this one. It trades at reduced size until the nightly sweep says otherwise.

Setup:
- Downtrend: close below the 50-day SMA
- A rally into the 20-day SMA that is **rejected** — closes back below it
- Short the failure, not the rally itself. Wait for the reclaim to fail

Risk:
- Stop above the swing high that rejected. Above that, the downtrend is in question
- Reward:risk at least 2.5, measured to the next support shelf, not to zero
- **A short's loss is unbounded and gaps do not respect stops.** Size smaller than
  the equivalent long. Never hold through the name's own earnings
- Check borrow: an expensive or hard-to-borrow name eats the edge before you start

Invalidation:
- Close back above the 50-day SMA — the downtrend premise is gone
- A reclaim of the 20-day SMA that holds for two closes
- Broad-market strength: the sweep is unambiguous that these signals lose to being
  passively long over a decade. A short is a bet against the drift, so it needs the
  regime behind it — check `get_market_context` before proposing

Prefer a put spread when the thesis is a dated catalyst: `propose_vertical(
direction="bearish")` caps the loss at the debit, is exempt from the event wall,
and cannot gap through a stop.

# Vol premium

**Unproven, and it cannot become proven by backtest.** There is no options price
history to replay, so this tag will never pass the nightly sweep and stays at 0.25x
until the live grading ledger says otherwise. That is the honest deal, not a
loophole — quarter size on an unvalidated idea is what every other tag gets.

## Why this one exists

Every other strategy in the registry is a long-vol directional trend bet. They want
the same tape, which is why they share a regime signature and fail in the same
cells. This one is the opposite bet: **short volatility, mean-reverting, and it
profits from price NOT moving.** That is the diversification — not another name, a
different exposure.

## Setup

Read `get_options_chain` for IV rank, then:

- **IV rank >= 70 and no binary event inside the expiry** -> SELL premium.
  `propose_vertical(structure="credit", direction=...)`. Rich IV with no known
  catalyst is the premium worth collecting.
- **IV rank <= 30 and a dated catalyst inside the window** -> BUY premium.
  `propose_vertical(structure="debit", direction=...)`. Cheap IV ahead of a known
  event is the premium worth owning.
- **Anything in between: no trade.** The middle of the IV-rank range has no edge in
  either direction and the spread will eat you.

Direction: lean with the trend when there is one. In a sideways tape lean *against*
the stretch — price extended above its 20-day SMA means sell the call side, extended
below means sell the put side.

## Risk

- Defined risk only, always. A credit vertical's max loss is the strike width minus
  the credit, and the guardrail recomputes it from your legs independently.
- **Never sell premium into a binary event.** IV is rich before earnings and an FOMC
  because the move is genuinely uncertain — collecting that premium is picking up
  pennies in front of the thing that prices it. The event veto is not optional.
- Credit spreads win often and lose big. A high hit rate here is not evidence of
  edge; the only number that matters is expectancy net of the losses.
- Size to the options max-loss cap, not to the credit received.

## Invalidation

- IV rank collapses back through the middle of the range before the structure has
  earned its theta — the premise (rich premium) is gone, take it off.
- A binary event appears inside the expiry after you are on. Close or roll past it.
- Price closes decisively through the short strike: this is a mean-reversion trade
  and the mean stopped holding.

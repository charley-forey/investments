# Playbook: options

Defined-risk options as a first-class expression of a thesis. The weekend research
agent proposes edits here based on the week's journal and per-strategy statistics.

## When options beat shares
A dated catalyst or a defined timeframe (earnings, product event, breakout expected to
resolve in days/weeks) where leverage + capital efficiency + capped risk beat holding
stock. If the thesis has no clock, prefer shares — theta bleeds a timeless option.

## Structures (all defined-risk; naked is blocked by the guardrail)
- **debit-call/put-vertical, long-call/put** — directional conviction. Buy premium
  when IV rank is low (cheap). Max loss = premium (long) or net debit (vertical).
- **credit-vertical** — sell rich IV (high IV rank) with a capped max loss = strike
  width x 100 - credit. Profit from decay + IV contraction. Avoid selling into a
  binary event unless collecting the crush IS the thesis and it's still defined.
- **csp-wheel** — cash-secured put to get paid bidding for shares you'd own anyway; if
  assigned, roll to a **covered-call** against the shares. Income + basis reduction.

## Reading the chain (get_options_chain gives these)
- **delta** — directional exposure & leverage per contract.
- **theta** — daily decay you pay (long) or collect (short); size it vs your horizon.
- **IV rank** — premium rich or cheap now vs its own history. Buy low, sell high.
- **put/call skew** — downside demand; a rich put skew makes put credit spreads or
  CSPs better paid.
- **spread / liquidity** — only trade tight, real markets; a wide spread eats the edge.

## Entry filters
- Underlying in the universe; a specific, dated reason the move resolves before expiry.
- IV rank consistent with the structure (cheap to buy, rich to sell).
- DTE >= the configured minimum; enough liquidity that the spread clears costs.
- Computed max loss within the options max-loss cap (guardrail recomputes independently).

## Exit rules
- Close/roll before the catalyst passes if it was the thesis; don't hold long premium
  through fast theta into expiry once the move has played out.
- Take profit on credit structures at a set fraction of max credit; don't hold for the
  last few dollars against tail risk.
- Manage assignment: an assigned short put becomes shares — decide covered-call vs exit.

## Known failure modes
- Buying rich IV before earnings and getting crushed even when directionally right.
- Selling premium into a binary event and eating the gap.
- Illiquid contracts: the modeled edge never survives the real spread.

## Lifecycle
Stage per strategy tag in the journal (kv_state stage:<tag>). Promotion to live
requires clearing the paper_to_live gates in limits.yaml.

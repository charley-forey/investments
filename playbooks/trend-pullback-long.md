# Trend pullback long

**The only strategy in this system with validated out-of-sample edge.** Prefer it.

Backtest: 1,280 trades, 2016-2026 daily bars, 10 symbols, 2% stop, production
friction. **+0.459 mean OOS R** across 40 walk-forward windows, positive in 80% of
them. Shadow ledger through 2026-07-28: **+$264.66 on 4 wins in 5** — the best of six
tags, and the only one that was also positive on both measures.
Full notes: `docs/backtest_trailing_exit_2026-07-28.md`.

Use when OpportunityScore flags `trend-pullback-long`.

Setup:
- Uptrend intact — fast SMA above slow SMA, price above the slow SMA
- Price has pulled back to within ~2% of the 20d SMA rather than extended away from it
- Still within ~10% of the trailing high; this buys a dip in a leader, not a downtrend
- Reclaim of the fast SMA is the trigger, not the touch — let it turn back up

Risk:
- Stop below the pullback low or the slow SMA, whichever is tighter
- **Reward:risk at least 2.5** (`limits.orders.min_reward_risk`), which is what the
  target study set it to
- No fixed take-profit. The exit is the trailing stop (`exits.trailing_pct`, 8%) —
  a resting target caps the winner and, measured, makes the trail dead code

Invalidation:
- Close below the slow SMA — the trend premise is gone, exit rather than widen
- A pullback that becomes a lower high: this is a continuation trade, not a reversal

Notes:
- Win rate is *low by design* (~28%). Expectancy rises as the win rate falls; the tail
  pays for the losers. Do not "improve" it by taking profits earlier — that was
  measured and it is strictly worse.

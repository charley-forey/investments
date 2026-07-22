# ORB / opening-range breakout

Use when OpportunityScore flags `orb-breakout` (range_break + elevated RVOL).

Setup:
- Liquid name, spread within limits
- Price breaks opening range high/low with RVOL >= ~1.2
- Prefer continuation with RS vs SPY supportive

Risk:
- Stop beyond the opposite side of the opening range (or VWAP if tighter)
- Do not chase >1 ATR past the break without a pullback hold

Invalidation:
- Immediate reclaim back inside the OR on rising volume

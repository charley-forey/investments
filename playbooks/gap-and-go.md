# Gap-and-go

Use when OpportunityScore flags `gap-and-go` (gap >= ~1.5% with RVOL).

Setup:
- Gap in the trade direction that holds the opening print / VWAP
- Relative volume confirms (no fading first 15–30m)
- Prefer liquid mid/mega caps from the scanner pool

Risk:
- Stop under gap-fill midpoint (longs) or above (shorts)
- Skip if earnings/FOMC inside 1 day unless the thesis IS the event

Invalidation:
- Full gap fill against the position on expanding volume

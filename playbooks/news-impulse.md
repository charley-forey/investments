# News impulse

Use when OpportunityScore flags `news-impulse` (fresh headlines + day move).

Setup:
- Fresh catalyst in intel/news (not stale recirculation)
- Price still respecting VWAP / structure after the print
- Defined risk; options debit spreads preferred when IV is not extreme

Risk:
- Size smaller into binary events; respect calendar proximity score
- Social heat alone is never enough — need price + volume confirmation

Invalidation:
- Headline reversed / rumor denied; structure breaks on the news bar

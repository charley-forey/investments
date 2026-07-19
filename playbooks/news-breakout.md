# Playbook: news-breakout

Seed playbook. The weekend research agent proposes edits here based on the week's
journal and per-strategy statistics; changes are tracked in git.

## Thesis
A liquid stock gaps or breaks a recent range on a concrete, fresh catalyst
(earnings, guidance, contract, regulatory news) and continues in the catalyst's
direction intraday.

## Entry filters
- Symbol in the configured universe, price > min_price, tight spread.
- A specific news item from the last 24h that plausibly moves the stock.
- Break of the prior N-day range confirmed on volume.

## Exit rules
- Hard stop below the breakout level (used for sizing).
- Take profit into strength or trail; do not hold through the next earnings event.

## Known failure modes
- "Buy the rumor, sell the news" reversals on already-priced-in catalysts.
- Thin pre-market prints that don't hold once the regular session opens.

## Lifecycle
Stage is tracked in the journal (kv_state stage:news-breakout). Promotion to
live requires clearing the paper_to_live gates in limits.yaml.

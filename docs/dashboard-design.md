# Operations console — design

What the console is for, why it is shaped this way, and what is deliberately not
built yet. The previous dashboard was ten flat tabs of tables plus a
`JSON.stringify` dump; this is a rebuild, not a restyle.

## The problem with tabs-of-tables

The data was all there and none of it answered a question. Specifically:

- **Ten peer tabs, no hierarchy.** You had to already know which tab held the
  answer. Nothing said "look here first."
- **Time was invisible.** 371 equity snapshots, 1,313 signal snapshots, 699
  heartbeats and 54 cost rows rendered as one crude SVG line and a pile of flat
  tables. The history existed and was never drawn.
- **No cross-links.** The data has a real entity graph —
  `proposal → verdict → order → fill → tax lot → score` — and the UI never
  walked it. A symbol in one table did not reach its news, its thesis, or its P&L.
- **Numbers with no reference.** "Gross leverage 0.8×" means nothing without the
  cap from `limits.yaml` beside it.
- **Config was a JSON textarea**, while Pydantic already knew every field's type,
  bound and default.

## Information architecture

Six surfaces, ordered by the question each answers, replacing the ten tabs:

| Surface | The question | Absorbed |
|---|---|---|
| **Cockpit** | Is it healthy, what needs me, what changed? | overview |
| **Portfolio** | What do I hold and what is it exposed to? | positions, trades, risk |
| **Markets** | What is the agent looking at? | signals, intelligence |
| **Agent** | Why did it act — or not? | opportunities, activity, decisions |
| **Performance** | Is this actually working? | performance / P&L, edge |
| **Control** | Change behaviour, safely. | config, actions |

Symbol and decision are *entities*, not tabs. Both open as drill-down sheets over
any view (`#/symbol/NVDA`, `#/decision/14`), so a symbol is one click from
anywhere and the route is linkable.

## Interaction model

- **Hash routes.** Every view and drill-down is bookmarkable and shareable.
- **One time range for the whole page.** Presets plus a drag-to-brush on the
  equity curve; every panel re-renders against the same slice. Never a per-chart
  filter (that pattern makes charts silently disagree).
- **⌘K palette.** Views, actions, symbols, and a free-text search over the
  agent's reasoning record.
- **Drill-down, never dead ends.** Symbols, proposals and cycles are links
  everywhere they appear.
- **Refresh holds the previous paint** at reduced opacity — no skeleton flash, no
  layout jump. Polling pauses when the tab is hidden, a sheet is open, or the
  config form has unsaved edits.

## Charts

Hand-rolled SVG in `static/js/charts.js` — no chart library, no build step, works
offline. Every chart carries a crosshair or per-mark tooltip *and* a table-view
twin, so no value is reachable only through colour or hover.

`line` (multi-series, crosshair, brush) · `candles` (OHLC + volume + SMA) ·
`bar` (horizontal/vertical, diverging) · `stack` · `heat` · `funnel` ·
`scatter` · `spark` · `bullets`.

The panels that did not exist before and carry the most weight:

- **Decision funnel** — symbols examined → proposals → cleared guardrails →
  submitted → filled → closed. The biggest drop is the real bottleneck, and it is
  the honest answer to "why so few trades?"
- **Which guardrails actually fire** — verdicts grouped by rule. Turns the
  constraint set from a config file into an observed fact.
- **Risk against configured limits** — every risk number as a bullet filling
  toward its cap from `limits.yaml`, rather than a bare figure.
- **Drawdown from peak**, with the circuit-breaker threshold drawn as a band.
- **Agent spend** vs realized P&L. The edge has to clear the API bill; nothing
  showed that before.
- **Signal heatmap** — every tracked symbol over time, metric-switchable.

### Colour

The validated data-viz reference palette. The eight categorical slots are used in
fixed order and never cycled or re-ranked. The set was run through the validator
against *this* console's surfaces (dark `#151a21`, light `#ffffff`) and passes the
lightness, chroma, CVD-separation, normal-vision and contrast gates in both modes.
Three light-mode slots sit under 3:1 contrast, so the table-view twin on every
chart is the required relief, not a nicety.

P&L polarity is a **status** encoding, always paired with a ▲/▼ glyph so colour
never carries the sign alone — plus a colour-blind mode that swaps green/red for
the validated blue/orange diverging pair.

## Config editor

`/api/config/schema` serves Pydantic's `model_json_schema()` alongside the inline
`#` comments harvested from the YAML — the file was already the documentation.
The form is generated from it, so every field gets its real control (checkbox,
bounded number, select, list), its constraint enforced client-side by the same
schema the engine validates against, and its help text.

Saving is a three-step gate: validate against the model → show a field-level diff
→ write a timestamped backup to `data/backups/config/` before overwriting. Editing
`limits.yaml` warns that these are the hard limits the model cannot override.

## Responsive

One layout, two shapes. Left rail becomes a bottom tab bar under 1024px. Tables
become stacked cards under 720px, where columns marked secondary fold away — the
values stay reachable in the drill-down sheet and the table view. The side drawer
becomes a bottom sheet. Long tables are page-capped with filter and "show more":
an 800-row table is a 40,000px page on a phone and nobody scrolls it.

## Was the agent right?

P&L only grades the trades the agent took. The Performance view also grades the
calls it *declined*: every vetoed or rejected proposal is replayed against what
the price actually did, so a veto that saved money and a veto that cost an entry
are told apart. `/api/outcomes` joins those counterfactual grades with realized
scores and the confidence-calibration report.

The important part is the empty state. Both tables start empty for legitimate
reasons — nothing has closed, or no proposal has aged past the grading threshold —
so the payload reports what the pipeline is waiting on, per item, with the date
each becomes gradable and whether the broker is reachable. "No data" and "not
eligible yet" are different answers and the UI says which. A button grades
whatever is ready without waiting for the end-of-day cycle; it is deterministic
and makes no model calls.

## Scale

The read paths that grow without bound are indexed (`Journal._indexes`), and the
two that did work proportional to data size were moved server-side:

- **`/api/trades`** issued one fills query per order. Now two queries total,
  regardless of order count.
- **`/api/signals`** shipped every snapshot to the browser so it could reduce
  them. Now `/api/signals/latest` does the latest-per-symbol reduction in SQL, and
  `/api/signals/grid` buckets the heatmap in SQLite and returns the shape the
  chart consumes. The metric name is interpolated into SQL, so it is whitelisted.

## Auth

The API fails closed. Loopback with no token configured stays open — that is the
existing single-user local workflow. Set `DASHBOARD_TOKEN` and every `/api`
request must present it (`X-Dashboard-Token`, `Bearer`, or cookie, compared with
`compare_digest`). A non-loopback request without a configured token is refused
outright, and `trading dashboard` refuses to bind a non-loopback host without one.

The shell and its static assets stay unauthenticated — they hold no data, and the
page has to load in order to ask for the token. This is one shared secret, not
identity; multi-user accounts and roles remain an M13 item.

## Deliberately not built

- **No WebSocket.** 20s polling is enough at this cadence; live streaming is worth
  it only once intraday tick data drives the page.
- **No build step, no framework, no dependency.** Four static files. Revisit if
  the view layer outgrows plain functions.
- **No virtualised tables.** Page-capping covers the current data volume; swap in
  virtualisation when a table genuinely needs thousands of visible rows.
- **Bars are cached daily-only.** Intraday timeframes pass through to the broker
  each request; `bars` is keyed by `(symbol, date)`.
- **No server-side pagination.** Endpoints take a `limit`; the cursor pagination
  that a hundred thousand orders would need can wait until there are.

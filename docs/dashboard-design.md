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

## Deliberately not built

- **No auth.** Still localhost-only and unauthenticated, as before. Remote access
  needs auth first — the action endpoints submit orders.
- **No WebSocket.** 20s polling is enough at this cadence; live streaming is worth
  it only once intraday tick data drives the page.
- **No build step, no framework, no dependency.** Four static files. Revisit if
  the view layer outgrows plain functions.
- **No virtualised tables.** Page-capping covers the current data volume; swap in
  virtualisation when a table genuinely needs thousands of visible rows.
- **Bars are cached daily-only.** Intraday timeframes pass through to the broker
  each request; `bars` is keyed by `(symbol, date)`.

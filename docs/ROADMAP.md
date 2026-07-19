# Roadmap — Milestones 3–14

Design and strategy for the phases beyond the foundation. Milestones 1–6 are
**complete and committed** (`41e9abf`, `28ecef8`, `8bf1bec`, `e7aab76`, `1a7f185`,
`bc27d30`): foundation → autonomous paper trading → learning loop → options+live →
unattended operation → production hardening. This document is the reference for
what's built (M3–M6 below) and what comes next (M7–M14); treat it as a living
spec, refined as each milestone begins.

**Status at a glance:** the system is functionally complete and "sealed tight" for
paper trading — but every milestone below M7 is about turning a correct, safe
single-account bot into a **best-in-class, enterprise-grade platform**. Nothing has
yet run against a real market or LLM; the first live paper run (keys in `.env`,
`trading run-once --cycle intraday`) gates the start of M7+.

Guiding principle throughout, unchanged: **the LLM proposes, deterministic code
disposes.** Learning, capital allocation, and execution decisions that can be
computed are computed in Python and enforced mechanically; the model's role is
research, thesis generation, and judgment within hard boundaries.

---

## Milestone 3 — Sentiment, Scoring & the Learning Loop

**Goal:** the system improves week over week without human tuning. Three
reinforcing layers: trade-level feedback (daily), strategy-level statistics
(weekly, deterministic), and playbook evolution (weekly, agent-proposed).

### 3.1 Scoring agent + memory (daily)
- **New:** `src/trading/agents/scoring.py`, `src/trading/agents/prompts.py` (add
  `SCORING_SYSTEM`).
- Runs in the `postclose` cycle (replaces the placeholder EOD note in
  `orchestrator._note_cycle`). For each trade closed today (from `tax_lots` +
  `fills`), grade against its original thesis: outcome vs. thesis, exit
  discipline, sizing quality, what the market actually did.
- Persist to the existing `scores` table (`journal.record_score` — add this
  method mirroring `record_verdict`) **and** append a one-line lesson to
  `memory/lessons.md`. The strategy agent already reads memory via `read_memory`
  at session start, closing the loop.
- Keep memory files small and deduplicated (one lesson per line, update rather
  than append duplicates) so they stay inside the cached system context cheaply.

### 3.2 Strategy tagging + weekly stats job (deterministic)
- **New:** `src/trading/analytics/stats.py`, `src/trading/analytics/lifecycle.py`.
- Every proposal already carries `strategy_tag`. The stats job computes, per tag,
  from the journal: trade count, win rate, average win/loss, expectancy,
  max drawdown, and **after-cost / after-tax expectancy** — reusing the tested
  `after_tax_expectancy()` and cost model in
  `src/trading/guardrails/account_math.py`. Tax rates come from
  `settings.tax` (already modeled, with `short_term_total` / `long_term_total`).
- **Lifecycle gates (the money-allocation logic):** strategies move through
  `candidate → backtest → paper → small-live → scaled`. Promotion/demotion is
  pure code against thresholds already in `config/limits.yaml`
  (`lifecycle.paper_to_live_min_trades`, `paper_to_live_min_expectancy`,
  `demote_after_losing_weeks`). Store each tag's current stage in the `kv_state`
  table (`journal.set_state("stage:<tag>", ...)`). The strategy agent is told
  each tag's stage and per-stage sizing ceiling in its user message; the
  guardrail engine remains the hard backstop.
- **New table:** `strategy_state` (or reuse `kv_state`) — stage, trades, rolling
  expectancy, last promotion/demotion timestamp.

### 3.3 Sentiment ingestion
- **New:** `src/trading/data/sentiment.py`; new tool `get_sentiment` in the
  registry.
- Reddit via `praw` (add to deps; `REDDIT_CLIENT_ID/SECRET` already in
  `.env.example`) over a configurable subreddit list; Alpaca news already wired
  via `broker.get_news`. Produce a compact per-symbol signal (mention counts,
  crude polarity, notable headlines) — a tool result, not a trading decision.
- Cache sentiment pulls (they're slow and rate-limited); store raw pulls so the
  backtester can replay them.

### 3.4 Backtest harness
- **New:** `backtest/` package — bar history to Parquet (DuckDB for queries; add
  `duckdb`, `vectorbt` or `backtesting.py` to deps), and a runner that applies a
  strategy's rules over history **using the same cost model as the guardrails**
  (`estimate_cost_usd`) so backtests don't flatter strategies.
- Output feeds the `candidate → backtest` lifecycle gate: a tag must clear a
  backtest expectancy bar before it's allowed to paper trade.

### 3.5 Playbooks + weekend research (agent-proposed, git-tracked)
- **New:** `playbooks/<tag>.md` (rules, filters, failure modes); weekend cycle in
  the scheduler (`settings.schedule.weekend_research_*` already exists).
- The research agent reviews the week's journal + stats and proposes playbook
  edits (tighter filters, new exit rules). Paper-stage edits auto-apply; live
  edits queue for approval. Git history becomes the system's growth record.

### 3.6 Notifications
- **New:** `src/trading/notify.py` — Discord/Telegram webhook (URL already in
  `.env.example`). Daily summary from the scoring agent; later, live-approval
  pings (M4).

**M3 exit:** seed synthetic closed trades → weekly stats computes per-tag
after-tax expectancy → a losing tag is auto-demoted → the scoring agent writes a
lesson the strategy agent reads next run. Daily Discord summary fires.

**M3 tests:** stats math vs hand-computed journals; lifecycle promote/demote
transitions; scoring writes to `scores` + memory; sentiment tool returns a
summary from a stub; backtest cost model matches live cost model.

---

## Milestone 4 — Options Execution & Going Live

**Goal:** trade defined-risk options end-to-end, and enable a small live account
behind the async approval gate. Validation for options already exists and is
tested; this milestone adds *execution* and the *live path*.

### 4.1 Multi-leg options execution
- **Modify:** `src/trading/broker/alpaca.py` (add `submit_option_order` using
  alpaca-py `MarketOrderRequest`/`LimitOrderRequest` with `OrderClass.MLEG` and
  `legs=[OptionLegRequest(...)]`), and `guardrails/engine.py` `_submit()` (remove
  the `options_execution` stub rejection once real submission works).
- **Per-leg quotes:** the cost model currently approximates option spread from
  est. premiums. Add real per-leg option quotes (`broker.get_option_quote`
  already exists) into `estimate_cost_usd` for accurate hurdle checks.
- OCC symbol construction/validation helper (the chain tool already parses OCC
  symbols; factor that into a shared util).

### 4.2 Live path + async approval  ✅ (inbound bot deferred)
- Live mode + `ALPACA_LIVE_*` keys (config selects by mode). The
  `pending_approval` queue and `cli.py approve` exist from M1.
- **Done:** outbound async approval delivery — a Discord ping with the proposal
  details and the `trading approve <id>` command fires the moment a live order
  queues (`engine._notify_pending`). `limits.live.auto_submit_below_usd` lets
  small live orders skip the gate.
- **Done:** lifecycle gates now gate real capital — only `small-live`/`scaled`
  tags may submit live (`strategy_stage` guardrail), and the stage's sizing
  fraction scales the position cap in code.
- **Deferred to M5:** the *inbound* approval path (a Discord bot token or a tiny
  authenticated endpoint that turns a phone reply into `approve`). Today approval
  is the `trading approve <id>` CLI command; the outbound ping tells you what's
  waiting. This needs a bot token, so it's grouped with M5's service work.

### 4.3 Position management
- Exit/stop management for open positions (stops, profit targets, option expiry
  handling) — currently proposals are entries; add a management pass in the
  intraday cycle that can propose `reduces_position=True` closes.

**M4 exit:** a defined-risk spread validates, gets risk-reviewed, executes on
paper; in live mode a 1-contract order queues, a phone approval releases it, and
nothing reaches the broker without approval. Kill switch + wash-sale + PDT still
enforced live.

**M4 tests:** multi-leg submission builds the correct alpaca request (mocked);
per-leg cost model; live-gate queue→approve→submit with a stub live broker;
position-management pass proposes correct closes.

---

## Milestone 5 — Unattended 24/7 Operation  ✅ (VPS migration + inbound bot remain)

**Goal:** the daemon runs for weeks without babysitting and recovers from
failures on its own. **Built (commit pending):** retry/backoff (`resilience.py`),
per-cycle Anthropic cost tracking (`cost.py` + `usage` table), watchdog + health
+ daily summary (`monitoring.py`), nightly rotated journal backup (`backup.py`),
real-time fill websocket (`broker/stream.py`), scheduler jobs (watchdog/summary/
backup), extended `trading status` + `watchdog`/`backup`/`stream` commands, and
`docs/DEPLOY.md` (NSSM + systemd). **Remaining:** actually provisioning the VPS
(operational, not code) and the inbound approval bot (needs a Discord bot token).

Original design notes:

### 5.1 Service-ification
- **Phase A (this PC):** NSSM wraps `trading daemon` as a Windows service
  (auto-start, restart-on-failure). Document the `nssm install` invocation in
  `docs/DEPLOY.md`.
- **Phase B (VPS):** same daemon on a small Linux box under `systemd`
  (`Restart=always`), secrets in env, for true 24/7 independent of the PC.

### 5.2 Resilience
- **Websocket fills:** replace/supplement the polling `sync_fills` with Alpaca's
  `TradingStream` for real-time fill/position updates (polling stays as the
  reconciliation backstop).
- Retry-with-backoff wrappers on all broker/Anthropic calls; the scheduler
  already isolates per-cycle failures via heartbeats.
- **Watchdog + heartbeat:** a lightweight external check (systemd timer or a
  second tiny process) reads the `heartbeats` table and alerts via
  `notify.py` if no successful cycle in N minutes. Daily "I'm alive + what I did"
  summary.
- Nightly journal DB backup (copy + rotate; the DB is the entire audit trail and
  tax record).

### 5.3 Cost & ops controls
- Anthropic token/cost tracking per cycle (log `usage` from responses to the
  journal); optional cheaper-model screening pass if cost climbs
  (config-driven model per agent/cycle).
- `trading status` extended with uptime, last successful cycle, per-cycle cost,
  and reconciliation state.

**M5 exit:** kill the daemon mid-day → the service restarts it → the next
scheduled cycle runs → a watchdog/summary notification confirms liveness. Runs
unattended across a full week; nightly backups present.

---

## Anthropic-native alternative (revisit after M2/M3 are proven)

The research/scoring half could move to **Managed Agents scheduled deployments**
(cron-fired cloud sessions with persistent memory stores) while execution stays
in the local guarded daemon that holds the broker keys. This removes the VPS for
the "thinking" half. Deferred deliberately — prove the local loop first; it's an
optimization, not a prerequisite.

## Cross-cutting hardening (folded into M6)

The items formerly listed here (partial-lot closes, idempotency, reconciliation
gate) are now part of Milestone 6 below.

---

## Milestone 6 — Production Hardening ("sealed tight")

**Goal:** close every gap between "correct in simulation" and "safe to fund."
Grouped by how much money a gap can lose or mislead. **Build-now** items are
implemented in this milestone; **M7** items are documented but deferred (they need
external data feeds, a bot token, or heavier infra).

### 6.1 Money-critical (build now)
- **Real protective orders.** Stops were computed for sizing but never placed.
  Opening long-stock orders with a `stop_price` (and optional `target_price`)
  now submit as **Alpaca bracket orders** (entry + stop-loss + take-profit,
  atomic) so a position is protected the instant it fills — not on the next scan.
- **Option fills tracked as tax lots.** `sync_fills` detects OCC option orders and
  records lots with a 100x contract multiplier, so options P&L flows into scoring,
  the lifecycle, and tax accounting instead of being invisible.
- **Partial-lot closes.** Selling part of a lot now splits it (a closed portion +
  a reduced remainder) for accurate per-lot basis and realized P&L, instead of
  closing the whole lot.
- **Working-order lifecycle.** Stale unfilled limit orders are cancelled after a
  configurable TTL at the start of each intraday cycle (`cancel_stale_orders`).

### 6.2 Correctness (build now)
- **Exact strategy attribution.** Orders submit with `client_order_id = prop-<id>`;
  sync maps a fill back to its proposal (and thus its `strategy_tag`) exactly,
  replacing the "most recent proposal for this symbol" heuristic that misattributed
  P&L when strategies shared a ticker.
- **Idempotent submission.** The deterministic `client_order_id` also makes order
  submission safe to retry — a crash mid-submit can't double-send.
- **Reconciliation as a gate.** A broker/journal position mismatch beyond tolerance
  sets a halt flag that blocks *opening* trades (closes still allowed) until
  resolved — no longer just a warning.
- **Backtest gate enforced.** A strategy at `candidate`/`backtest` stage (sizing
  fraction 0) is now rejected by the guardrails in every mode; a passing backtest
  promotes `candidate → paper`. Un-vetted strategies can't trade.

### 6.3 Intelligence & capability (build now)
- **Portfolio-level risk.** New guardrails beyond per-position: max gross exposure
  (Σ|market value| / equity) and max concurrent positions per underlying — so the
  book can't quietly become one concentrated bet.
- **Market-context tool.** `get_market_context` gives agents a computed regime read
  (SPY trend + realized volatility) so proposals account for the tape, not just the
  single name.

### 6.4 Deferred to M7 (need external data / infra / tokens)
- **Wash-sale cost-basis deferral** (currently blocks the re-buy; full treatment
  defers the disallowed loss into the replacement lot's basis) + broker lot
  instructions on close + year-end realized-gains / 1099 reconciliation export.
- **Sector/correlation risk** (needs a sector/industry data feed).
- **Fill-quality analytics** (estimated vs. realized slippage trend dashboards).
- **Bar-history persistence** (Parquet + DuckDB store) so backtests don't re-fetch,
  plus walk-forward and benchmark-relative backtesting of the agents' *actual*
  proposed strategies and option structures.
- **Richer sentiment** (a real model instead of the keyword lexicon; optional X/Twitter).
- **Inbound approval bot** (Discord bot token) for phone-reply approvals.
- **Secrets manager** for live VPS deployment; structured metrics/observability.

**M6 exit:** a funded position always carries a live stop; option trades appear in
`stats`; partial exits report correct P&L; a candidate strategy is refused until
backtested; a position-concentration attempt is rejected; a seeded reconciliation
mismatch halts new entries. All verified by unit tests.

---

# Phase Two — Milestones 7–14 (best-in-class / enterprise)

**Build status (all committed, 198 tests green):**
- **M7 Tax & Compliance** — ✅ built (wash-sale deferral, realized-gains export, harvesting)
- **M8 Data & Backtesting** — ✅ built (bar store, benchmark metrics, walk-forward auto-gate)
- **M9 Advanced Risk** — ✅ built (vol-target/Kelly sizing, VaR/ES, drawdown circuit breaker, regime scaling, correlation concentration)
- **M10 Signal & Alpha** — ◑ scaffolding built (features, pluggable sentiment seam, calendar tool); **blocked** on external paid data feeds (fundamentals, options-flow, real sentiment model)
- **M11 Multi-Agent Intelligence** — ✅ built (cost-aware model routing, adversarial red-team pass)
- **M12 Execution Quality** — ✅ built (limit placement, slicing, slippage tracking + self-calibrating hurdle)
- **M13 Observability & Ops** — ◑ scaffolding built (JSON logging, metrics snapshot, approval-command handler); **blocked** on a Discord bot token, secrets-manager backend, Postgres instance
- **M14 Governance & Scaling** — ✅ built (capital allocation, P&L attribution, human-gated live-scaling ladder)

Remaining is external, not code: the M10/M13 data feeds, tokens, and infra above,
plus the first live paper run (keys in `.env`). Everything is verified against
mocked Anthropic/Alpaca clients.

Sequencing principle: each milestone must leave the system **shippable and safe**,
and the "LLM proposes, deterministic code disposes" invariant holds throughout —
every new capability is either a computed guardrail/analytic (deterministic) or a
new tool/context the agents *weigh* within hard boundaries. Rough dependency order,
but M7 (tax) and M8 (data/backtest) can proceed in parallel; M9–M12 build on M8;
M13–M14 are the enterprise/scale layer.

## Milestone 7 — Tax & Compliance Completeness

**Goal:** the system's tax accounting matches the broker's and survives an audit.
Closes the M6-deferred tax items.

- **Wash-sale cost-basis deferral.** Today the wash-sale rule only *blocks* a re-buy
  (`guardrails/engine.py` § wash_sale). Full treatment: when a loss is disallowed,
  defer it into the replacement lot's cost basis and extend its holding period —
  implemented in the `tax_lots` layer (`data/journal.py`), with a `wash_sale_flag`
  already on the schema. Handle the 30-day window in both directions and
  substantially-identical securities (incl. options on the same underlying).
- **Broker lot instructions on close.** The journal already selects HIFO lots; send
  those lot IDs to Alpaca on the closing order so the broker's 1099 matches our
  books (extend `broker/alpaca.py` order submission).
- **Year-end exports.** Realized-gains CSV (short/long term) and a 1099
  reconciliation report that diffs our `tax_lots` against the broker's realized
  activity; a December tax-loss-harvesting review job (new `analytics/tax.py`).
- **After-tax everything.** Already have after-tax expectancy; extend the daily
  summary and `trading stats` with realized-tax-drag and harvesting opportunities.

**Exit:** a full simulated year of trades produces a realized-gains export whose
totals reconcile to the (paper) broker; a wash sale correctly defers basis rather
than just blocking; unit tests cover both-direction windows and options.

## Milestone 8 — Data & Backtesting Platform

**Goal:** replace the toy backtester with a real research platform so strategy
promotion (`candidate → backtest → paper`) is driven by rigorous evidence.

- **Bar-history persistence.** Parquet store + DuckDB query layer (deps noted in
  the original plan) under `data/bars/`; an ingestion job that caches daily (and
  later intraday) bars so backtests and features don't re-fetch.
- **Strategy-faithful backtesting.** Today `backtest/` replays only SMA/breakout on
  equities. Build an engine that backtests the **agents' actual proposed strategy
  logic** and **option structures**, using the same guardrail cost model
  (`friction_cost`) and the same sizing rules — so a backtest is a faithful
  simulation of the live path, not a different code path.
- **Rigor:** walk-forward / out-of-sample splits, parameter-stability checks, and
  benchmark-relative metrics (alpha, beta, Sharpe/Sortino, max drawdown vs SPY).
- **Auto-gate wiring.** A passing walk-forward result auto-calls
  `lifecycle.promote_after_backtest`; a failing one demotes — the research loop
  becomes self-driving within hard thresholds.

**Exit:** a strategy the agent invents is backtested end-to-end on cached history,
scored against SPY with walk-forward, and promoted or rejected automatically.

## Milestone 9 — Advanced Risk & Portfolio Construction

**Goal:** move from per-position limits to portfolio-theory-grade risk.

- **Sector/industry/correlation data** (needs a data feed — GICS or similar) to
  power correlation-aware concentration limits (beyond M6's per-underlying count).
- **Risk-based sizing upgrades:** volatility targeting, fractional-Kelly, and
  correlation-adjusted position sizing in `guardrails/account_math.py`.
- **Portfolio risk metrics:** value-at-risk / expected shortfall, gross/net/beta
  exposure by regime (using M6's `get_market_context`), and factor exposure.
- **Dynamic guardrails:** tighten gross exposure and per-trade risk automatically in
  elevated-volatility regimes; a portfolio-level drawdown circuit breaker layered
  above the daily kill switch.

**Exit:** sizing reflects volatility and correlation; a correlated cluster of
positions is capped as one bet; risk limits tighten in a stressed regime — all
deterministic and tested.

## Milestone 10 — Signal & Alpha Expansion

**Goal:** widen the opportunity set with better inputs (all delivered to agents as
tools/context, never as autonomous decisions).

- **Real sentiment model** replacing the keyword lexicon (`data/sentiment.py`):
  a transformer/finance-tuned classifier or a sentiment API; optional X/Twitter.
- **New signal tools:** options-flow / unusual-activity, fundamentals (earnings,
  margins, valuation), an earnings/economic-calendar tool (event-risk awareness),
  and multi-timeframe technical features.
- **Feature store:** computed features persisted alongside bars (M8) so backtests
  and live decisions read identical feature definitions.
- **Strategy universe growth:** the weekend research agent proposes new tagged
  candidate strategies from these signals; M8's backtester vets them.

**Exit:** the agent can cite a fundamentals + options-flow + calendar-aware thesis;
new candidate strategies enter the lifecycle from research and get backtested.

## Milestone 11 — Multi-Agent Intelligence Upgrade

**Goal:** deepen the "multi-agent" system from the original brief and cut cost.

- **Specialized agents:** macro/regime, sector-rotation, options-strategist, and an
  execution-trader, coordinated by the orchestrator — the roster the resources.txt
  brainstorm envisioned.
- **Debate/critique loops:** strategy ↔ risk already exist; add an adversarial
  "red-team" pass on high-conviction ideas, and the **advisor tool** pattern (a
  stronger model advising a cheaper executor) for hard calls.
- **Cost-aware model routing:** cheap model for screening/watchlists, top model for
  final decisions (config-driven per agent/cycle — the `cost.py` hooks exist).
  Prompt-caching audit + tool-search for the growing tool set to keep context lean.
- **Optional:** move research/scoring to **Managed Agents scheduled deployments**
  (cloud, persistent memory stores) while execution stays in the guarded local
  daemon that holds the keys.

**Exit:** a measurable cost-per-decision drop with equal-or-better decision quality;
specialized agents contribute distinct, attributable proposals.

## Milestone 12 — Execution Quality & Microstructure

**Goal:** stop leaking edge at the point of execution.

- **Smart limit placement:** price inside the spread, adaptive repricing of working
  orders (extends M6's stale-order handling), and liquidity-aware sizing.
- **Order slicing:** TWAP/VWAP for orders large relative to average volume.
- **Fill-quality analytics:** the M6-deferred est-vs-realized slippage tracking —
  record it per fill, trend it in a dashboard, and feed realized costs back into the
  cost hurdle so the model tightens over time.
- **Partial-fill & rejection handling:** first-class states in the order lifecycle.

**Exit:** measured slippage vs. arrival price is tracked and trending down; large
orders are sliced; the cost hurdle self-calibrates from realized fills.

## Milestone 13 — Observability, Ops & Reliability at Scale

**Goal:** enterprise-grade operations and the deferred human-in-the-loop pieces.

- **Structured logging + metrics** (Prometheus/Grafana or a hosted dashboard) beyond
  the heartbeats table; alert escalation beyond a single Discord webhook.
- **Secrets manager** (Vault / cloud KMS) replacing plaintext `.env` for live.
- **Inbound approval bot** (Discord bot token): turn a phone reply into
  `trading approve <id>` — the last piece of true async phone approval.
- **Datastore scaling:** migrate the journal from SQLite to Postgres if write
  concurrency (daemon + stream + workers) demands it; keep the repository interface
  (`data/journal.py`) stable so the swap is contained.
- **HA / DR:** redundant deployment, automated failover, tested restore from the
  nightly backups, and immutable audit/compliance logging.

**Exit:** a metrics dashboard is live; a leaked-key drill and a DB-restore drill both
pass; approvals happen from a phone; the daemon runs multi-region.

## Milestone 14 — Governance, Live Scaling & Continuous Improvement

**Goal:** run a *portfolio of strategies* with real, growing capital under formal
governance — the end-state the whole project points at.

- **Capital allocation across strategies:** allocate equity by proven, risk-adjusted
  after-tax expectancy (extends the M3 lifecycle); rebalance on the weekly job.
- **Automated strategy A/B & versioning:** playbook versions (already git-tracked)
  run head-to-head; winners scale, losers retire — governed by hard statistical
  thresholds, not vibes.
- **Performance attribution:** decompose P&L by strategy, signal, regime, and
  execution so you know *why* it's making money.
- **Graduated live-scaling policy:** an explicit, code-enforced ladder for raising
  the guardrail ceilings as the track record lengthens — the only place a human
  raises limits, with a required review gate.
- **Continuous research pipeline:** research → backtest (M8) → paper → small-live →
  scaled runs as a standing loop, expanding and pruning the strategy universe
  autonomously within its boundaries.

**Exit:** capital is allocated across multiple proven strategies by the numbers;
P&L is fully attributable; scaling live size requires a documented, gated human
review; the research pipeline runs continuously and self-prunes.

---

## Cross-cutting, always-on (apply within every milestone)

- **Safety invariant:** the guardrail engine remains the sole path to the broker;
  no new feature lets the LLM bypass it. New risk controls are code, not prompts.
- **Test-first:** every deterministic component ships with unit tests; the suite
  stays green at each milestone (currently 117 tests).
- **Paper-before-live:** capability lands in paper, earns its lifecycle stage, and
  only then touches live capital — enforced by the `strategy_stage` guardrail.
- **Cost discipline:** track Anthropic spend per cycle (M5) and per decision (M11);
  route models by task.
- **Auditability:** the journal is the immutable record of every proposal, verdict,
  order, fill, score, and tax lot — extend it, never bypass it.

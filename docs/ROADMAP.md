# Roadmap — Milestones 3–5

Design and strategy for the remaining phases. Milestones 1 (foundation) and 2
(agent loop + autonomous paper trading) are complete and committed (`41e9abf`,
`28ecef8`). This document is the reference for what comes next; treat it as a
living spec, refined as each milestone begins.

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

## Milestone 5 — Unattended 24/7 Operation

**Goal:** the daemon runs for weeks without babysitting and recovers from
failures on its own.

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

## Cross-cutting hardening (fold in as milestones progress)

- **Partial-lot closes:** M2 fill sync closes whole lots; add proportional
  splitting for accurate per-lot basis and wash-sale precision.
- **Idempotency everywhere:** fill sync is idempotent; extend the same
  discipline to any new external-effect path (order submission dedupe on
  client-side order IDs).
- **Reconciliation as a gate, not just a warning:** once trusted, a hard
  broker/journal mismatch should block new trading until resolved.

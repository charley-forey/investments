# Design & Roadmap — Whole-System Assessment and Level-Up Strategy

> Authoritative, living design document. Inventories every capability of the agentic
> trading system, assesses each honestly, and sets the forward strategy to make each
> best-in-class. Pairs with `ROADMAP.md` (milestone history M1–M24); this doc is the
> **capability-oriented** view and the go-forward plan.

**North-star invariant (unchanged — and the reason this is safe to fund):**
*The LLM proposes, deterministic code disposes.* Every order passes the guardrail
engine; the model never holds broker keys. Every enhancement below is either a computed
guardrail/analytic (deterministic) or a tool/context the agents *weigh* within hard
boundaries.

**Maturity legend:** **Solid** (production-grade) · **Working** (correct but shallow) ·
**Scaffolded** (seam built, real feed/logic pending) · **Gap** (missing).

**Scale today:** ~75 modules · 13 subsystems · 358 passing tests · live on Alpaca paper
with Claude agents + OpenAI embeddings.

---

## Part 1 — Capability inventory, assessment & per-subsystem roadmap

### 1. Orchestration & scheduling — *Solid*
**What it is:** `orchestrator.py` runs four cycle types (premarket watchlist, intraday
propose→risk→execute, postclose scoring, weekend research), now including deterministic
position management, volatility-targeted sizing, the per-interval signal snapshot, and
narrative capture. `scheduler.py` is the APScheduler daemon; `config.py` is the typed
config over `limits.yaml`/`settings.yaml`.

**Gaps:** fixed-cadence only — no event-driven triggers (news spike, opening gap, vol
regime change); every cycle deep-scans all 23 universe names (cost + dilution of focus).

**Roadmap:** event-driven intraday triggers; a **screen → deep-dive funnel** (a cheap
model ranks the 23 names, the top model deep-dives the top 3) to cut cost and sharpen
focus; regime-conditioned cadence (scan more often in volatile tapes, less in quiet).

### 2. Agents & intelligence — *Solid (breadth) / Working (depth)*
**What it is:** `agents/` — strategy, independent risk, adversarial red-team, scoring,
and market-intel curation agents; `runner.py` (tool loop + summarized-thinking capture);
frozen `prompts.py` (cache-friendly); cost-aware per-role model routing (`model_for`).

**Gaps:** one generalist strategy agent (the original brief envisioned specialists —
macro/regime, sector-rotation, options-strategist, execution-trader); no offline way to
evaluate a prompt/model change before it goes live; adaptive-thinking effort not tuned to
cycle difficulty; no advisor-tool pattern (a strong model advising a cheap executor).

**Roadmap:** a **specialist agent roster** coordinated by the orchestrator; an
**agent-decision eval harness** (replay history, score the agent's actual decisions vs a
benchmark policy) so prompt/model changes A/B *before* live; effort routing (low for a
premarket screen, high for a live entry); a tokens-per-decision cost metric.

### 3. Tools — *Solid*
**What it is:** `tools/registry.py` — account/quote/bars/features/intraday-features/
options-chain (greeks+IV+skew)/sentiment/news/calendar/market-context/market-intel/
recall-similar/propose-order. Read-only tools return compact text; `propose_order` only
registers a draft (execution is the orchestrator's job, behind risk + guardrails).

**Gaps:** no fundamentals tool; no options-flow tool (seam only); no one-call
multi-name screen/compare; no dedicated position-detail tool for exits; tool results
aren't cached within a cycle (the same quote can be re-fetched).

**Roadmap:** fundamentals + real-calendar + options-flow tools; a `screen_universe`
tool returning a ranked table in a single call; a per-cycle tool-result cache.

### 4. Guardrail engine & risk math — *Solid* (the crown jewel)
**What it is:** `guardrails/engine.py` — the sole path to the broker. Enforces kill
switch, peak-to-trough drawdown circuit, strategy-stage gate, PDT, wash-sale, notional /
per-position / max-open / gross-exposure / **net-delta** / correlation caps, options
defined-risk + independent max-loss recompute + DTE + contract caps, the cost hurdle, and
a reconciliation halt. `account_math.py` does option-leg analysis and sizing.

**Gaps:** net-delta uses stock delta only (existing-option greeks not aggregated); no
portfolio vega/theta cap; correlation is price-history-derived (no sector data).

**Roadmap:** **greeks-aware portfolio caps** (delta/vega/theta) pulling existing-option
greeks from the snapshot/chain; a scenario/stress soft-warn (±σ underlier move → book
P&L); per-sector concentration once a sector feed lands.

### 5. Broker & execution — *Solid / Working*
**What it is:** `broker/alpaca.py` (equities, intraday bars, multi-leg options,
news, clock), `sync.py` (partial-fill-aware fill sync + reconciliation), `stream.py`
(websocket fills), `occ.py`; `execution.py` (limit placement, slicing, realized
slippage) + `execution_pricing.py` (marketable-limit, mid-peg) + deterministic exits.

**Gaps:** *entry* orders still use the agent's naive limit (smart pricing is wired only
into exits); no TWAP/VWAP slicing on the live path; option exits are *flagged* for the
agent, not auto-managed (needs a multi-leg close/roll executor); no adaptive repricing
of resting orders.

**Roadmap:** thread smart pricing into `engine._submit` for entries; a **multi-leg
option close/roll executor** so deterministic exits act on options too; adaptive
repricing of working orders; liquidity-aware sizing from real volume.

### 6. Data & persistence — *Solid / Scaffolded*
**What it is:** `journal.py` (the immutable audit trail: proposals/verdicts/orders/
fills/lots/scores/reasoning/snapshots/outcomes; WAL + busy_timeout for concurrency),
`bars.py` (bar store), `intel.py` (news/sentiment), `vectors.py` + `memory_vectors.py`
(semantic memory), `snapshot.py` (per-interval research dataset), `flow.py` (options-flow
seam), `calendar_feed`/`calendar_provider`, `ingest.py`.

**Gaps:** SQLite single-writer ceiling (fine at one box, caps future concurrency);
sentiment is a keyword lexicon (real-model seam unfilled); flow / fundamentals / real
calendar are stubs; intraday bars aren't persisted (re-fetched each cycle); no feature
store (backtest-vs-live feature parity is by convention, not enforced).

**Roadmap:** unblock the seams (finance-tuned sentiment model, options-flow feed,
earnings/fundamentals feed); persist intraday bars + a **feature store**; plan the
SQLite→Postgres migration behind the stable journal interface.

### 7. Analytics & signals — *Solid (compute) / Working (breadth)*
**What it is:** `features` (daily technicals), `intraday` (VWAP/opening-range/rel-vol),
`options` + `options_term` (greeks/IV/skew/term structure), `regime`, `portfolio_risk`,
`sizing` (vol-target + drawdown throttle), `risk` (VaR/ES/correlation), `stats`,
`allocation`, `scaling`, `lifecycle`, `scorer`, `counterfactuals`, `calibration`,
`edge` (proof-of-alpha), `tax`, `reconcile`, `decision_record`, `signal_research`.

**Gaps:** features are daily + basic-intraday (no fundamentals/flow-derived factors);
regime's VIX/breadth inputs are computed but not yet fed into the *live* read; IV-rank is
still accumulating history; no cross-sectional ranking (relative strength across names).

**Roadmap:** wire **VIX + breadth into the live regime read**; cross-sectional factor /
relative-strength ranking; surface IV-rank + term-structure signals in the snapshot and
decisions.

### 8. Backtesting & research — *Working / Gap (fidelity)*
**What it is:** `backtest/` (dependency-free bar-replay engine using the *same* friction
model as the guardrails; walk-forward; Monte Carlo; benchmark metrics), `edge.py`
(t-stat proof-of-alpha + SPY benchmark), `signal_research.py` (sentiment→forward-return
studies), `paper_proof.py` (readiness gate).

**Gaps — the single biggest fidelity gap in the system:** the backtester replays only
SMA/breakout baselines, **not the agents' actual proposed strategies or option
structures**. So the `candidate → backtest` promotion gate doesn't vet what the agent
really does; there's no intraday or options backtest; paper-vs-backtest reconciliation is
thin.

**Roadmap:** a **strategy-faithful backtester** that replays the agent's codified
rules + option structures through the same guardrail cost/sizing path; an options
backtest; regime-conditioned + walk-forward as the standing auto-promotion gate; the
agent-decision eval harness (shared with §2).

### 9. Learning loop — *Solid* (genuinely differentiated)
**What it is:** `scorer` (deterministic per-lot scoring), the scoring agent (qualitative
lessons → `memory/lessons.md`), `counterfactuals` (grade *passed* trades — did the stop
or target hit?), `calibration` (are confidence numbers meaningful? is the risk agent too
strict? which guardrail binds?), `playbooks` (git-tracked strategy evolution), semantic
memory (`recall_similar`), `signal_research`. Most retail systems never grade non-trades.

**Gaps:** lessons/outcomes aren't regime-tagged; no closed-loop check that a lesson
actually changed behavior or improved outcomes; calibration/counterfactuals aren't
surfaced in the dashboard.

**Roadmap:** **regime-tag** every proposal/outcome/lesson; a **lesson-efficacy metric**
(did adherence to lesson N improve expectancy?); surface calibration + counterfactual +
edge on the dashboard so the learning is auditable.

### 10. Observability & UX — *Solid*
**What it is:** `web/app.py` + the SPA (Overview, Positions + portfolio-risk panel,
Trades, Opportunities, Activity narrative log, Signals grid, Performance, Intelligence,
Decisions drill-down, Config editor), `decision_record` (thesis→reasoning→verdicts→
order→fill→score), `monitoring`, `notify`, `logging_setup`.

**Gaps:** read-mostly — no interactive agent chat ("why did you…", "what if…"); no
benchmark-overlay equity/drawdown chart; no calibration/counterfactual/edge/IV-rank
views; no alert-config UI; localhost-only (no auth for remote/mobile).

**Roadmap:** interactive decision chat; benchmark + drawdown overlays; **learning-loop
dashboards** (calibration, counterfactuals, edge, IV-rank); backtest-from-dashboard;
auth + mobile view for remote use.

### 11. Ops & reliability — *Solid / Scaffolded*
**What it is:** the daemon (`scheduler`), `resilience` (retry/backoff), `backup`
(nightly rotated journal copy), `monitoring` (heartbeat watchdog + daily summary),
`preflight` + `paper_proof` (go/no-go), the 24h cost cap, `approvals`.

**Gaps:** single box; no HA/DR or tested restore drill; secrets in `.env`; alerting is a
single Discord webhook; the inbound phone-approval bot needs a token; no structured
metrics export (Prometheus/Grafana).

**Roadmap:** VPS/systemd deploy; secrets manager; metrics export + escalation; inbound
approval bot; DB-restore and leaked-key drills.

### 12. Tax & compliance — *Solid*
**What it is:** `tax.py` — wash-sale cost-basis deferral (real IRS treatment, both-
direction window, options on the same underlier), HIFO lot selection, tax-loss
harvesting, realized-gains export; lots carry the 100× contract multiplier.

**Gaps:** no broker-lot instructions on close (so the broker 1099 may not match our
HIFO books); no year-end 1099 reconciliation diff; corporate actions (splits/dividends/
mergers) unmodeled.

**Roadmap:** send HIFO lot IDs on close; a 1099 reconciliation report; corporate-actions
handling; realized-tax-drag + harvesting opportunities in the daily summary.

### 13. Capital & governance — *Solid / Working*
**What it is:** `allocation` (capital by realized, risk-adjusted, after-tax expectancy
with a sample-size haircut), `scaling` (human-gated live-scaling ladder), `lifecycle`
(candidate→backtest→paper→small-live→scaled gates), the live approval gate.

**Gaps:** idle cash sits (no T-bill/MMF sweep); allocation is per-strategy, not whole-
book tax-aware rebalancing; no multi-account; margin/interest unmodeled.

**Roadmap:** idle-cash sweep; tax-aware whole-book rebalance; margin/interest modeling;
optional multi-account — the capital-management end-state.

---

## Part 2 — The five biggest levers (highest impact, regardless of order)

1. **Strategy-faithful backtesting + agent-decision eval (§8/§2).** Until the backtester
   vets the agent's *actual* structures, "proven edge" isn't truly proven and every
   prompt change flies blind. The highest *trust* lever.
2. **Intraday signal depth wired live + real data feeds (§6/§7).** Better inputs → better
   decisions. Wire the computed regime/intraday signals into live decisions and unblock
   the sentiment/flow/fundamentals/calendar seams.
3. **Execution completeness (§5).** Smart entry pricing + a multi-leg option exit/roll
   executor — stop leaking edge and manage the *whole* book deterministically.
4. **Greeks-aware portfolio risk (§4).** Aggregate delta/vega/theta caps as options
   scale — the one place current risk is thinner than the options ambition.
5. **Learning made visible & regime-aware (§9/§10).** Regime-tagged lessons + dashboards
   showing calibration/counterfactuals/edge, so the learning loop is auditable, not a
   black box.

---

## Part 3 — Prioritized sequence

**Now (0–2 weeks) — all internal, no paid feeds; each ships with tests + a live run-once:**
- Strategy-faithful backtest of the agent's actual proposed structures (§8).
- Wire VIX + breadth into the live regime read (§7).
- Thread smart entry pricing into `engine._submit` (§5).
- Surface edge / calibration / counterfactual / IV-rank on the dashboard (§10).

**Next (mid-term):**
- Agent-decision eval harness (§2/§8).
- Specialist agent roster + screen→deep-dive funnel (§1/§2).
- Multi-leg option exit/roll executor (§5).
- Greeks-aware portfolio delta/vega/theta caps (§4).
- Regime-tagged learning + lesson-efficacy metric (§9).

**Later (infrastructure & external data):**
- Real feeds: finance-tuned sentiment model, options-flow, fundamentals, earnings
  calendar (§6).
- SQLite → Postgres; VPS/systemd + secrets manager + metrics/alerting + inbound approval
  bot (§11).
- Interactive dashboard chat + auth/mobile (§10).
- Idle-cash sweep + tax-aware whole-book rebalance + corporate actions (§12/§13).

---

## Part 4 — "Is it all working?" — validation plan

- **Test floor:** the full suite (currently **358** passing) green is non-negotiable; add
  coverage for each newly live-wired path (regime inputs, entry pricing, greeks caps).
- **`trading doctor`:** a new command extending `preflight`/`paper_proof` that exercises
  every subsystem end-to-end against the paper account and prints a per-capability
  **green / amber / red** board — the single "is everything healthy?" check.
- **The real proof:** a 30-day unattended paper run — fills synced, lots/scores correct,
  the edge report trending, zero crashes. Live capital only after a strategy clears the
  walk-forward gate *and* confirms in weeks of paper.

---

## Guiding principles (apply to every change)

- **Safety invariant:** the guardrail engine stays the sole path to the broker. New risk
  controls are code, never prompts.
- **Paper before live:** capability lands in paper, earns its lifecycle stage, then
  touches live capital — enforced by the `strategy_stage` guardrail.
- **Deterministic where computable:** sizing, allocation, exits, and risk are computed
  and enforced in Python; the model's role is research, thesis, and judgment.
- **Auditability:** the journal is the immutable record of every proposal, verdict,
  order, fill, score, and tax lot — extend it, never bypass it.
- **Cost discipline:** track Anthropic spend per cycle and per decision; route models by
  task difficulty.

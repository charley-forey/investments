# Critical Review — Skeptical Audit Before Real Capital

> An adversarial, evidence-grounded review of the whole system: prioritization,
> gaps, correctness, blind spots, adversarial cases, scaling, and proof-of-edge.
> Written to be uncomfortable. Where a claim has a code receipt it's cited `file:line`.

**The one-line honest verdict:** the system is broad, well-architected, and
well-*unit-tested* — but **nothing in it is production-*proven*, because it has ~0 real
fills and ~0 closed trades.** Every "Solid" rating means "correct in simulation," not
"survived a real market." The roadmap slightly under-weights safety/correctness
hardening relative to visibility. Details below.

---

## A. Prioritization & weighting

### A1. Weighted scoring matrix
Weights: **impact on risk-adjusted returns 40% · safety/trust 30% · cost-to-build 20%
(scored as *cheapness*: 5 = days, 1 = weeks+external) · dependency-unblocking 10%.**
Each item scored 1–5; weighted total = 0.4·I + 0.3·S + 0.2·C + 0.1·D.

| # | Item | I | S | C | D | **Score** |
|---|------|---|---|---|---|-----------|
| A | Strategy-faithful backtester | 4 | 5 | 2 | 4 | **3.9** |
| F | Multi-leg option exit/roll executor | 3 | 5 | 3 | 1 | **3.4** |
| B | Wire VIX+breadth into live regime | 3 | 3 | 5 | 2 | **3.3** |
| L | Reconciliation + broker-truth hardening | 2 | 5 | 4 | 2 | **3.3** |
| M | Runtime freshness/assumption checks | 2 | 5 | 4 | 1 | **3.2** |
| C | Smart entry pricing into submit path | 4 | 2 | 4 | 1 | **3.1** |
| E | Agent-decision eval harness | 3 | 4 | 2 | 3 | **3.1** |
| G | Greeks-aware portfolio caps | 2 | 4 | 3 | 1 | **2.7** |
| I | Real data feeds (sentiment/flow/fundamentals) | 4 | 2 | 1 | 3 | **2.7** |
| K | Regime-tagged learning + lesson efficacy | 2 | 2 | 4 | 2 | **2.4** |
| H | Specialist agent roster + funnel | 3 | 2 | 2 | 1 | **2.3** |
| D | Learning-loop dashboards | 1 | 3 | 4 | 1 | **2.2** |
| J | SQLite → Postgres | 1 | 3 | 2 | 2 | **1.9** |

### A2. Where this disagrees with Part 3 of the roadmap
Part 3's **"Now"** = A, B, C, **D**. The matrix says:
- **Delete D (dashboards) from "Now."** It scores **2.2 — near the bottom.** Visibility
  is trust-adjacent but moves neither returns nor safety; it's comfort, not a gate.
- **Promote F (option exits) and L+M (reconciliation + freshness checks) into "Now."**
  They rank #2/#4/#5 and are *absent from Part 3's Now entirely* (the roadmap files
  reconciliation/robustness under "Later"). For a system about to touch real capital,
  **safety/correctness hardening should outrank a dashboard.** This is the review's main
  structural disagreement with the roadmap.
- **Revised "Now":** A (backtester) → L+M (reconciliation/freshness) → F (option exits)
  → B (regime). C (smart pricing) is a fast-follow; D drops to "Next."

### A3. If we could ship only three before going live
1. **Strategy-faithful backtest + a computed edge-proof gate (A).** *Skipped →* you fund
   a strategy whose "edge" is variance you mistook for skill; you scale losers with real
   money. Largest expected loss in the system.
2. **Reconciliation + runtime freshness/assumption checks (L+M).** *Skipped →* the
   journal silently diverges from broker truth; PDT, wash-sale, tax, and sizing decisions
   run on wrong state; a stale/halted quote sizes a position wrong. You learn via a margin
   call, not an alert.
3. **Option position lifecycle: exits, roll, assignment, expiry (F).** *Skipped →* a short
   put assigned overnight becomes a surprise 100-share long into a gap; an ITM option
   expires unmanaged; option exits are currently only *flagged*, never executed
   (`orchestrator._manage_positions` acts on stocks only).

Smart pricing and dashboards are **not** gates.

### A4. Dependency graph & critical path
```
feature-store / bar persistence ─► strategy-faithful backtester (A)
        (real feeds I) ──────────►        │
                                           ▼
                            walk-forward promotion gate
                                           │
                                           ▼
                               edge proof (edge.py, ≥N trades)
                                           │
                                           ▼
                        lifecycle gate ─► capital allocation ─► live scaling
```
- **Critical path to real capital:** *feature store → faithful backtester (A) → walk-forward
  gate → edge proof → live sizing.* Nothing downstream (allocation, scaling ladder) is
  trustworthy until A is real. **A is load-bearing for E (eval harness), the promotion
  gate, and every "proven-edge" claim.**
- **Not on the graph but gate-for-live:** L, M, F are *prerequisites*, not dependencies —
  parallelizable with A.
- **J (Postgres)** blocks only the multi-worker/specialist-roster concurrency scaling —
  low centrality; defer.

### A5. What to delete from the roadmap
- **Specialist agent roster (H)** — speculative. One generalist + independent risk +
  red-team already covers the roles; a 4-agent roster adds cost and coordination for
  unproven marginal quality. YAGNI until the generalist is measurably the bottleneck.
- **Kelly sizing (`account_math.kelly_fraction`, `kelly_cap`)** — fractional-Kelly on a
  ~0-trade sample is noise dressed as math. Deactivate until there's a real track record.
- **`iv_adjusted_size` (sizing.py)** — built, speculative, unused; delete until options
  volume justifies it.
- **Interactive dashboard chat** — the decision records already answer "why."
- **Multi-account / margin-interest modeling** — premature at one small account.
- **SQLite→Postgres** — defer (not delete): `busy_timeout` bought real headroom; migrate
  only when concurrency actually binds.

---

## B. Gaps & blind spots

### B1. One trade lifecycle — where data is lost / assumed / never checked
- **Premarket scan:** watchlist is written to a *single overwritten* memory file — prior
  watchlists are **lost** (no history). Sentiment is a keyword lexicon **assumed** to be
  signal. Quote freshness is **never checked** (a stale/one-sided quote is used; there's a
  >1% spread warning in `_t_get_quote` but the agent may ignore it).
- **Proposal:** an option's `est_premium` is the agent's **estimate**, not a live quote —
  and the guardrail max-loss recompute uses it, so a wrong estimate → wrong max-loss.
  `confidence` is self-reported and **uncalibrated**.
- **Risk verdict:** LLM judgment — **non-deterministic** and not reproducible; the exact
  data the risk agent saw is **not recorded**.
- **Guardrails:** net-delta aggregates **stock delta only** — existing options' delta is
  ignored, so book directional risk is **understated**. All caps use `ref_price =
  quote.mid`; a stale quote **silently** propagates into every cap.
- **Fill:** polling sync is partial-fill-aware and attributes by `client_order_id`
  (good), but **manual/external account changes are unattributed**, and a fill outside the
  `list_orders_since` window can be **missed** until reconcile catches the drift.
- **Exit:** deterministic exits act on **stocks only**; options are flagged, not managed.
  **Trailing stops are silently inert** — `ExitRules.trailing_pct` defaults off because no
  high-water mark is persisted, so a "trailing stop" the operator might assume is active
  **never fires**.
- **Scoring:** only **closed** lots are scored; open-position experience is not learned
  from. Counterfactuals grade passes on **daily bars** — intraday stop/gap paths are
  **assumed away**.
- **Lesson:** free-text, appended to `lessons.md`, **not regime-tagged**, with **no check
  that it changed behavior**. A wrong lesson is read every cycle thereafter (§E1).

### B2. What the system assumes true but never verifies at runtime
- Broker positions == journal lots (only reconciled *at sync*, tolerance 1 share; between
  syncs, decisions trust the journal).
- Quotes/bars are **fresh** — no timestamp-age check; a halted symbol's last print is used
  as live.
- `est_premium` ≈ real option fill.
- No overnight corporate action changed share counts (a split silently breaks qty math).
- The Alpaca data plan returns greeks/IV (degrades to `—`, but net-vega/IV-rank go
  **silently absent**, not flagged).
- `kv_state` (kill-switch, `equity_peak`) isn't corrupted.
- Clock/timezone agreement (mostly mitigated — cycles gate on `broker.market_open()`).

### B3. Failure modes no guardrail/test/alert covers — ranked by expected $ damage
1. **Funding a no-edge strategy** (backtester not faithful + tiny sample). *$$$*
2. **Stale/halted-quote sizing** — wrong size or bad fill, options worst (wide spreads). *$$*
3. **Option assignment/expiry unmanaged** — surprise exposure/gap; pin risk. *$$*
4. **Broker/journal drift on un-reconciled dimensions** — dividends/cash and any fill the
   sync window misses accumulate silently. *$$*
5. **Trailing stops believed-active but inert.** *$*
6. **Sustained broker-API outage mid-position** — cannot exit; positions ride; alerting is
   one webhook with no escalation. *$*

### B4. Paper vs live surprises
- **Fill quality:** paper fills at/near mid with no queue/slippage — **live is worse,
  options much worse.** Any edge measured on paper is **inflated.**
- **Options assignment/exercise:** early assignment on ITM shorts, pin risk — **not
  modeled**; paper behavior differs from live.
- **Halts / LULD bands:** paper doesn't halt; live does.
- **PDT:** paper enforcement differs; live hard-blocks the 4th day trade.
- **Borrow/locate for shorts, price improvement, routing, rate limits** — all differ.

### B5. Worst-day traces (what happens *today*)
- **Flash crash:** daily-loss kill switch (blocks new entries, allows closes) + 15%
  drawdown circuit both exist and trip on equity; **stocks carry bracket stops** (protected
  the instant they fill). **But** existing positions are only re-evaluated every 15 min, and
  **options have no protective stop and exits are only flagged** → options ride the crash
  uncovered; a gap through a stock bracket fills worse than the stop. *Stocks partially
  protected; options exposed.*
- **Broker API down mid-position:** sync fails (caught, noted); `with_retry` covers transient
  blips; a **sustained** outage = blind and unable to exit until it returns. No escalation.
- **LLM provider outage during market hours:** the strategy/risk agents fail (caught), but
  the cycle's **deterministic** parts still run — `sync_fills`, `_manage_positions` (stock
  stops), and the snapshot. **So stock stops still fire without the LLM** — a genuine
  graceful-degradation strength. Options still unmanaged.

---

## C. Errors & correctness

### C1. Silent-failure surface
- **79 `except Exception` blocks across 22 files**; **25 in `orchestrator.py` alone.** Most
  are deliberate best-effort (snapshot, notify, narrative) that append to `report.notes` —
  but `report.notes` lands only in a heartbeat detail string, **not an alert**, so a
  *persistently* failing subsystem (e.g., every snapshot failing) degrades **silently** to
  anyone not reading the DB.
- **Money is `float` everywhere** — no `Decimal`. Penny rounding is mostly benign but wrong
  at the margins of tax lots / max-loss / net-limit option pricing. A known, accepted corner
  — should be documented as such, ideally `Decimal` on the tax/P&L path.
- **`or 0` / `getattr(...,None)` defaults** mask missing data (e.g., a `None` bid becomes
  `0`, which then reads as a valid-but-zero price downstream).

### C2. Where two parts compute the same thing differently
- **Two `vol_target_size` implementations, different semantics:**
  `guardrails/account_math.py:42` (M9 — $-cap sizing, tested in `test_m9_risk.py`) vs
  `analytics/sizing.py:14` (Track C — weight/target-vol, tested in `test_trackC_portfolio.py`).
  The **live path uses only the `analytics/sizing` one** (`orchestrator.py:303`); the
  `account_math` version is **effectively orphaned but still tested** — two "sizing truths,"
  one dead, both green. Real confusion/divergence risk; consolidate or delete the M9 one.
- **Corrected assumption:** backtest friction does **NOT** diverge from live — `backtest/
  engine.py:15` imports `friction_cost` from `guardrails.account_math`. Single-sourced.
- **Delta math is single-sourced** — the net-delta guardrail reuses
  `analytics.portfolio_risk` (wired that way deliberately). Good.

### C3. Weak tests (mocks over behavior; thin coverage vs blast radius)
- **StubBroker/ScriptedClient dominate** — agent tests never exercise a real LLM tool loop;
  broker tests use a stub returning a fixed `99.9/100.1` quote, so **stale-quote, halt,
  None-field, and real partial-fill paths are untested.**
- **Trivial assertions** exist (`assert not journal.kill_switch_active()`).
- **Untested, high-blast-radius paths:** reconciliation drift accumulation, option
  assignment, the *inert* trailing stop, the two-`vol_target_size` divergence, and the
  **real** daemon+dashboard concurrent-writer race (`db_util` is tested in isolation, not
  against the live contention it exists to solve).
- **Well-covered (rightly):** the guardrail engine — highest blast radius, best tested.

### C4. Reconciliation drift — what accumulates undetected, and for how long
`broker/sync.py::_reconcile` diffs broker vs journal positions with `tolerance_shares` and
sets a halt on mismatch — but only for **share-quantity** on symbols it sees. **Undetected:**
cash/dividend drift (never reconciled), a fill outside the sync window (until the next sync
catches the position delta), and anything on a dimension other than share count. A corporate
split would over-shoot tolerance and **halt** (safe but then stuck until manual resolution).
Caught drift: resolved within a cycle (minutes). Un-reconciled dimensions (cash): **drift
indefinitely.**

---

## D. Second-order, adversarial, scaling, regulatory

### D1. Learning-loop second-order — can a bad lesson poison decisions?
**Yes, and it's unmitigated.** A lesson is free text appended to `memory/lessons.md`, read
by the strategy agent **every cycle**. An overfit or small-sample lesson biases **all**
future decisions until a human removes it. There is **no efficacy check, no A/B, no
automated rollback.** Detection today = a human noticing degraded performance (or the
calibration report, which measures confidence/veto quality, **not** lesson impact).
Rollback = manual `git revert` of `lessons.md`. Playbooks are git-tracked (auditable,
revertible — better); `lessons.md` just accretes. **Mitigation:** tag each lesson with
sample-size + date, cap the count, and gate a lesson's continued influence on its measured
subsequent efficacy — promote/retire lessons like strategies.

### D2. Adversarial / abuse cases
- **Prompt injection via news/sentiment text — real, unmitigated.** `search_news` and the
  intel digest feed **external attacker-controllable text** straight into the agent context.
  A crafted headline could attempt to steer a proposal. **The guardrail engine is the
  backstop** — an injected proposal still must clear notional/defined-risk/net-delta/stage
  caps, so injection **cannot bypass risk limits**, but it could waste cost or push a
  marginal in-limits bad trade. *Fix:* label tool-returned text as untrusted data; never let
  it read as instructions.
- **Poisoned memory recall** — `recall_similar` can resurface a poisoned stored item; same
  bounded risk.
- **Leaked API key — highest severity for live.** `.env` plaintext, no secrets manager. A
  live-broker key leak = real money; an Anthropic/OpenAI key leak = cost abuse. Unmitigated
  beyond filesystem perms.
- **Dashboard control endpoints are unauthenticated** (`run-cycle`, `approve`,
  `reset-kill-switch`, `config-save`) — safe **only** because bound to `127.0.0.1`. Any
  port-forward/0.0.0.0 bind = remote control of the trader. Keep it localhost; add auth
  before any remote use.

### D3. Scaling 23 → 100 names / 1× → 10× capital — what breaks first
1. **Cost (first).** 100 names × ~26 cycles/day on the top model → the $15/day cap trips
   constantly and pauses the agent. The **screen→deep funnel stops being optional.**
2. **Rate limits (second).** 100 quotes+bars+chains/cycle; snapshotting 100 option chains
   is heavy → Alpaca data limits.
3. **Focus/quality (third).** One agent over 100 names dilutes decision quality.
4. **Slippage at 10× capital.** Naive entry limits + larger size → market impact the
   paper account never shows; execution quality becomes material.
5. **SQLite (later).** ~2,600 snapshot rows/day + more writers; `busy_timeout` delays the
   ceiling but doesn't remove it.
6. **Caps bind.** Gross/net/correlation caps tuned for small size will reject more —
   correct, but needs re-tuning.

### D4. Regulatory / tax / broker-ToS not modeled (beyond corporate actions)
- **Options tax:** assignment/exercise lot creation, Section 1256 (index options 60/40) —
  not modeled.
- **Cash-account settlement (T+1), good-faith / free-riding violations, Reg-T margin** —
  not modeled (PDT *is*).
- **NIIT 3.8%, AMT, state-specific rules, wash-sale on options** — simplified in `tax.py`.
- **Cross-account wash sales (IRA)** — not handled.
- **Options approval tiers / entitlements, data-redistribution ToS** — assumed.

---

## E. Measurement & proof

### E1. Minimum evidence of real edge — and how far we are
**Required:** positive expectancy in **walk-forward out-of-sample** *and* confirmed in live
paper, on a sample large enough to reject luck — practically **~100 closed trades per
strategy** (a t-stat CI on expectancy excluding 0; Sharpe with a CI above 0; expectancy
clearing modeled costs by a margin; stable across walk-forward folds; **paper matches
backtest** within tolerance). `edge.py` already computes a t-stat but with `MIN_TRADES=20`
— that's a **floor, not proof** (20 trades is easily luck).
**How far:** the system has **~0 closed trades** and the backtester **can't yet** produce the
faithful OOS side. So **edge is not computable today and won't be for weeks–months** at the
current low proposal rate. *This is the real gate to live capital — everything else is
secondary to closing it.*

### E2. Evidence behind each "Solid" rating — proven vs self-assessed
| Subsystem | Basis | Honest status |
|---|---|---|
| Guardrail engine | Extensive unit tests | **Mostly proven** (in sim); never met a real adverse fill/halt |
| Learning loop | Logic tested | **Self-assessed** — never shown to *improve* decisions |
| Tax | Tested vs hand-computed | **Self-assessed** — never reconciled to a real 1099 |
| Ops/reliability | Watchdog/backup tested | **Self-assessed** — no restore drill, no completed multi-week run |
| Broker/execution | Stub-order tests | **Partly self-assessed** — no real partial-fill/reject-at-volume validation |
| Tools/analytics | Unit tests | Logic proven; live-data edge cases unproven |

**Reframe:** "Solid" = *correct in simulation and well-tested.* **Zero subsystems are
production-proven**, because the system has no real fills yet. The design doc's ratings
should be read through that lens.

### E3. A skeptical quant's first five attacks (and our answer)
1. **"Show me the out-of-sample edge."** — No faithful backtest + ~0 live trades → **no
   answer.** *(The central weakness.)*
2. **"Your paper fills are fantasy."** — Paper >> live fill quality, options especially →
   measured edge inflated → **no good answer** until live paper/real fills.
3. **"Backtest ≠ live code path."** — *Partly false* (friction is shared) but sizing has
   two implementations and the backtester runs baselines, not the agent's logic → **weak
   answer.**
4. **"An LLM in the loop is non-deterministic and injectable."** — True; guardrails backstop
   it, but news-injection is unhandled and decisions aren't reproducible → **partial.**
5. **"Show me portfolio tail risk in a stress, not per-trade limits."** — Gross/net/drawdown
   caps + VaR exist, but no scenario stress, no greeks-aware book risk, correlation from
   thin history → **partial.**

---

## F. The five things this review would do first (net recommendation)
1. **Make the backtester faithful + wire the edge-proof gate** (A, E1) — the only path to
   honestly justifying live capital.
2. **Reconciliation + runtime freshness/broker-truth checks** (L, M, B2) — stop decisions
   running on stale/wrong state.
3. **Option lifecycle: exits, roll, assignment, expiry** (F, B4) — close the biggest
   unmanaged-exposure hole.
4. **Consolidate the two `vol_target_size`; add a `trading doctor` health board; make
   silent-failure runs alert** (C1, C2) — kill the correctness/observability blind spots.
5. **Lesson-efficacy gating + treat tool text as untrusted** (E1/D1, D2) — protect the
   learning loop from self-poisoning and the agent from injection.

Everything here preserves the invariant: the guardrail engine stays the sole path to the
broker, and it is — proven in simulation — the strongest part of the system. The work above
is about making the *evidence* as strong as the *architecture*.

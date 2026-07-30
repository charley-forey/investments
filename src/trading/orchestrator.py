"""Orchestrator: one cycle = snapshot -> sync -> strategy agent -> risk review ->
guardrail pipeline. This is the wiring that turns agents into (paper) trades.

Dependency injection on the constructor keeps it fully testable with scripted
agents and a stub broker.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import cost, notify
from .agents import intel as intel_mod
from .agents import redteam as redteam_mod
from .agents import risk as risk_mod
from .agents import scoring as scoring_mod
from .agents import strategy as strategy_mod
from .analytics import lifecycle
from .analytics.scorer import score_closed_trades
from .analytics.stats import open_positions_summary, portfolio_summary
from .broker.sync import cancel_stale_orders, sync_fills
from .config import Config
from .data.journal import Journal
from .guardrails.engine import OrderPipeline
from .guardrails.models import OrderProposal

_BILLING_HALT_KEY = "llm_billing_halt"
_BILLING_RETRY_MINUTES = 30.0

# Budget pacing. A flat 0.25 share per hour was the first attempt and it still
# guaranteed a blind afternoon: four hours at the cap consumes a 6.5-hour session,
# and on 2026-07-29 it did exactly that ($3.89/$3.89/$3.75/$3.16, done by 13:00 ET,
# then "skipped: cost cap reached" every minute to the close — through the FOMC
# decision, which is the single most informative hour of that day).
#
# The share now derives from how much of the session is actually left, so spend
# spreads across it by construction. The floor stops a quiet morning from handing
# one hour an unbounded allowance.
_MIN_HOURLY_BUDGET_SHARE = 0.15
# Reserved for the postclose learning cycle, which shares this budget and lost
# every contest against intraday: it ran at cost=$0.000 on 07-29 and has not
# produced a scoring row since at least 07-26. Intraday cannot spend this.
_POSTCLOSE_RESERVE_USD = 0.75
# Session bounds (ET) used only for pacing arithmetic.
_SESSION_OPEN_HOUR = 9
_SESSION_CLOSE_HOUR = 16


@dataclass
class CycleReport:
    cycle: str
    skipped: str | None = None
    proposals: int = 0
    vetoed: int = 0
    submitted: int = 0
    rejected: int = 0
    pending_approval: int = 0
    cost_usd: float = 0.0
    notes: list[str] = field(default_factory=list)

    def summary(self) -> str:
        # `notes` MUST be rendered. ~40 `except Exception` blocks in this file
        # append there and nothing printed them, so a failure left no trace
        # anywhere: the market digest 400'd on every premarket cycle for six
        # days (2026-07-23 to 07-29) and the only symptom was a stale page.
        # A swallowed exception that is also unlogged is not error handling.
        suffix = f" | {'; '.join(self.notes)}" if self.notes else ""
        if self.skipped:
            return f"[{self.cycle}] skipped: {self.skipped}{suffix}"
        return (f"[{self.cycle}] proposals={self.proposals} vetoed={self.vetoed} "
                f"submitted={self.submitted} rejected={self.rejected} "
                f"pending={self.pending_approval} cost=${self.cost_usd:.3f}{suffix}")


class Orchestrator:
    def __init__(
        self,
        config: Config,
        journal: Journal,
        broker,
        client,
        *,
        strategy_runner=strategy_mod.run_strategy_session,
        risk_reviewer=risk_mod.review_proposal,
        red_team_reviewer=redteam_mod.red_team,
    ):
        self.config = config
        self.journal = journal
        self.broker = broker
        self.client = client
        self.pipeline = OrderPipeline(config, journal, broker)
        self._run_strategy = strategy_runner
        self._review = risk_reviewer
        self._red_team = red_team_reviewer
        self._score = scoring_mod.run_scoring_session

    def run_cycle(self, cycle: str = "intraday") -> CycleReport:
        report = CycleReport(cycle=cycle)
        self.journal.heartbeat(f"cycle:{cycle}", status="start")

        # Truthful journal first (best-effort; a sync failure shouldn't abort EOD).
        try:
            sync_fills(self.config, self.journal, self.broker)
        except Exception as e:
            report.notes.append(f"sync failed: {e}")

        if cycle in ("intraday",):
            if self.journal.kill_switch_active():
                report.skipped = "kill switch active"
                self.journal.heartbeat(f"cycle:{cycle}", status="skip", detail=report.skipped)
                return report
            # NB the cost cap is NOT checked here. It gates the LLM only, and it used
            # to sit on this line -- which meant that when it tripped (10:30am on
            # 2026-07-28, 208 times) stop/target/trailing/expiry management and stale
            # order cancellation stopped running too. Exits are deterministic and free;
            # they must never be priced out. The check now lives in _trade_cycle, just
            # above the strategy agent.
            try:
                if not self.broker.market_open():
                    report.skipped = "market closed"
                    self.journal.heartbeat(f"cycle:{cycle}", status="skip", detail=report.skipped)
                    return report
            except Exception as e:
                report.notes.append(f"market check failed, proceeding: {e}")

        account = self.broker.get_account_state(self.journal)
        try:
            self.journal.record_equity(equity=account.equity, cash=account.cash,
                                       buying_power=account.buying_power)
        except Exception:
            pass

        if cycle == "intraday":
            self._trade_cycle(account, report)
        elif cycle == "postclose":
            self._postclose_cycle(account, report)
        elif cycle == "weekend":
            self._weekend_cycle(account, report)
        else:
            self._note_cycle(cycle, account, report)

        self.journal.heartbeat(f"cycle:{cycle}", status="end", detail=report.summary())
        try:
            notify.notify_cycle(self.config, self.journal, report)
        except Exception as e:  # notifications never break a cycle
            report.notes.append(f"notify failed: {e}")
        return report

    # -- intraday: propose -> risk -> execute --------------------------------

    def _trade_cycle(self, account, report: CycleReport) -> None:
        # Clear out stale working orders before proposing anything new.
        try:
            n = cancel_stale_orders(self.config, self.journal, self.broker)
            if n:
                report.notes.append(f"cancelled {n} stale order(s)")
        except Exception as e:
            report.notes.append(f"stale-order cancel failed: {e}")

        market_open = True
        try:
            market_open = self.broker.market_open()
        except Exception:
            pass

        # Deterministic exit management (no LLM): close positions that hit a
        # stop/target/trailing/time rule or options nearing expiry, BEFORE the agent
        # proposes anything — so the strategy agent sees an already-managed book.
        try:
            self._manage_positions(account, report, market_open)
        except Exception as e:
            report.notes.append(f"manage_positions failed: {e}")

        # Everything above this line is deterministic and free. Everything below can
        # call the LLM, so the daily budget gates here — after the book is managed.
        if self._cost_capped():
            report.skipped = "cost cap reached"
            report.notes.append("LLM skipped: cost cap reached (exits still ran)")
            self.journal.heartbeat("cycle:intraday", status="skip", detail=report.skipped)
            return
        if self._billing_halted():
            report.skipped = "LLM billing halt (backing off)"
            report.notes.append("LLM skipped: billing halt (exits still ran)")
            self.journal.heartbeat("cycle:intraday", status="skip", detail=report.skipped)
            return

        # Cost gate: skip the expensive strategy LLM unless a trigger fires,
        # a position needs attention, or we're in a forced situational-awareness slot.
        from .triggers import should_run_intraday_llm
        gate = should_run_intraday_llm(self.config, self.journal, self.broker, account)
        if not gate.run_llm:
            report.skipped = gate.reason
            report.notes.append(f"LLM skipped: {gate.reason}")
            # The universe snapshot is its own scheduled job. It used to run here,
            # which made a skipped cycle cost 88 quotes and 88 rows — affordable at
            # a 15-minute cadence, ruinous now that this runs every minute to drain
            # the tick stream's wake queue.
            return
        report.notes.append(f"LLM gate: {gate.reason}")

        # Current regime — used to condition strategy selection on where each
        # template has actually shown edge, and passed to the agent as context.
        regime_trend = regime_vol = None
        try:
            from .tools.market_context import market_regime
            reg = market_regime(self.broker)
            regime_trend, regime_vol = reg.trend, reg.vol_state
        except Exception:
            pass

        stages = lifecycle.stages_summary(self.journal)
        perf = portfolio_summary(self.journal, self.config.settings.tax)
        extra = f"Strategy stages: {stages}\n{perf}"
        # What ten years of replay says about each strategy. The live ledger is two
        # closed positions deep; this is the only statistically meaningful evidence
        # the agent has, so it goes in beside the live numbers rather than behind a
        # tool call it has no reason to make.
        try:
            from .analytics.sweep import sweep_context

            sw = sweep_context(self.journal)
            if sw:
                extra += f"\n{sw}"
        except Exception:
            pass
        try:
            from .analytics.candidate_grading import regime_context
            rc = regime_context(self.journal, regime_trend, regime_vol)
            if rc:
                extra += f"\n\n{rc}"
        except Exception:
            pass
        # The same question answered from ten years of replay rather than from the
        # live ledger's handful of single-regime rows. Both are shown: they measure
        # different things (shadow hit rate vs R against a passive hold) and
        # disagreement between them is information.
        try:
            from .analytics.sweep import regime_context as bt_regime_context
            br = bt_regime_context(self.journal, regime_trend, regime_vol)
            if br:
                extra += f"\n\n{br}"
        except Exception:
            pass
        digest = self._intel_digest()
        if digest:
            extra += f"\n\nMarket intelligence digest:\n{digest}"
        try:
            from .scanner.movers import candidate_context, load_candidates
            cand = candidate_context(self.config)
            if cand:
                extra += f"\n\n{cand}"
                templates = {
                    c.get("template") for c in load_candidates(self.config)[:5]
                    if c.get("template")
                }
                if templates:
                    extra += (
                        "\nSuggested playbook templates for candidates "
                        f"(prefer these strategy_tags when they fit): "
                        f"{', '.join(sorted(t for t in templates if t))}."
                    )
        except Exception as e:
            report.notes.append(f"candidate context failed: {e}")
        try:
            session = self._run_strategy(
                self.client, self.config, self.journal, self.broker, account,
                cycle="intraday", extra_context=extra,
            )
        except Exception as e:
            # Losing analysis is survivable; losing the rest of the cycle is not.
            # Deterministic exit management has already run above.
            report.notes.append(f"strategy agent failed: {e}")
            self._alert_llm_down(e)
            return
        # A call got through, so any prior billing halt is stale.
        if self.journal.get_state(_BILLING_HALT_KEY):
            self.journal.set_state(_BILLING_HALT_KEY, "")
        self._record_usage("intraday", "strategy", session.usage, report)
        report.proposals = len(session.drafts)

        # Persist the agent's per-interval narrative (what it examined, why it
        # proposed nothing) — the reasoning table keyed by proposal_id=NULL. This is
        # thrown away otherwise on a 0-proposal cycle, which is most of them.
        try:
            self.journal.record_reasoning(
                proposal_id=None, agent="cycle:intraday",
                reasoning=session.final_text, tool_calls=session.tool_calls,
            )
        except Exception as e:
            report.notes.append(f"cycle narrative not recorded: {e}")

        for draft in session.drafts:
            # Tag discovery source from active scanner candidates / core universe.
            self._tag_discovery(draft)
            # Volatility-targeted sizing: shrink an oversized stock entry to a
            # risk-appropriate size (never grow it), then apply a drawdown throttle.
            self._risk_size(draft, account, report,
                            regime=(regime_trend, regime_vol))

            # Deterministic same-day re-pitch suppression: an unchanged entry idea
            # (same symbol+side+strategy_tag) already vetoed/rejected today is
            # dropped before spending another risk-agent LLM call on it. Exits are
            # never suppressed. The prompt rule alone gets ignored (GOOGL 7/23).
            if not draft.reduces_position and self.journal.repitched_today(
                    draft.symbol, draft.side, draft.strategy_tag):
                report.vetoed += 1
                pid = self._journal_veto(draft, "repitch_guard", risk_mod.RiskVerdict(
                    verdict="veto",
                    reason=f"same-day re-pitch: {draft.side} {draft.symbol} "
                           f"({draft.strategy_tag}) was already vetoed/rejected today",
                    concerns=[],
                ))
                self._record_reasoning(pid, session)
                continue

            # Regime conditioning: skip an entry whose template has a real adverse
            # track record in the current tape (enough samples, sub-coinflip hit
            # rate). No-op until the graded ledger has evidence — safe by default.
            if not draft.reduces_position:
                from .analytics.candidate_grading import REGIME_SKIP_HIT_RATE, regime_edge
                edge = regime_edge(self.journal, draft.strategy_tag,
                                   regime_trend, regime_vol)
                if edge and edge["hit_rate"] < REGIME_SKIP_HIT_RATE:
                    report.vetoed += 1
                    pid = self._journal_veto(draft, "regime_guard", risk_mod.RiskVerdict(
                        verdict="veto",
                        reason=f"{draft.strategy_tag} hit rate {edge['hit_rate']:.0%} in "
                               f"{regime_trend}/{regime_vol} over {edge['n']} graded "
                               f"samples (< {REGIME_SKIP_HIT_RATE:.0%}) — wrong regime",
                        concerns=[],
                    ))
                    self._record_reasoning(pid, session)
                    continue

            would_be_dt = self._would_be_day_trade(draft)
            verdict = self._review(
                self.client, self.config, self.journal, self.broker, account, draft
            )
            # The risk review is a billed LLM call per proposal — meter it, or the
            # daily cost cap is enforced against a fraction of real spend.
            self._record_usage("intraday", "risk", verdict.usage, report,
                               model=self.config.settings.agents.model_for("risk"))
            if not verdict.allows_trade:
                report.vetoed += 1
                pid = self._journal_veto(draft, "risk_agent", verdict)
                self._record_reasoning(pid, session)
                continue
            # 'amend' = the concern is size, not the setup. Take the trade smaller
            # rather than not at all; mechanical guardrails still run below.
            draft = verdict.scaled(draft)

            # High-conviction trades get an adversarial red-team pass; a veto here
            # skips the trade even though risk approved it.
            if redteam_mod.should_red_team(self.config, draft):
                rt = self._red_team(
                    self.client, self.config, self.journal, self.broker, account, draft
                )
                self._record_usage("intraday", "redteam", rt.usage, report,
                                   model=self.config.settings.agents.model_for("redteam"))
                if rt.verdict != "approve":
                    report.vetoed += 1
                    pid = self._journal_veto(draft, "redteam", rt)
                    self._record_reasoning(pid, session)
                    continue

            # Pre-authorised: risk approved the plan, but it waits for its level.
            # The tick stream fires it in milliseconds when price crosses, running
            # this same pipeline at that moment against a live account and quote.
            if draft.is_armed_plan:
                try:
                    pid = self._arm_plan(draft, verdict)
                    report.notes.append(
                        f"armed {draft.symbol} {draft.arm_direction} {draft.arm_level:g} "
                        f"(plan {pid})")
                    self._record_reasoning(pid, session)
                except Exception as e:
                    report.notes.append(f"arm {draft.symbol} failed: {e}")
                continue

            # Approved by risk -> run the deterministic guardrail pipeline.
            quote = self._quote_for(draft)
            try:
                result = self.pipeline.process(
                    draft, account, quote,
                    market_is_open=market_open, would_be_day_trade=would_be_dt,
                )
            except Exception as e:
                # A broker rejection must not abort the cycle: position management
                # and the remaining stages still need to run.
                report.notes.append(f"submit {draft.symbol} failed: {e}")
                continue
            # Attach the risk approval + captured strategy reasoning to the proposal.
            self.journal.record_verdict(
                result.proposal_id, source="risk_agent", verdict=verdict.verdict,
                reason=verdict.reason + (f" | amended to {verdict.qty_mult:.2f}x size"
                                         if verdict.verdict == "amend" else ""),
            )
            self._record_reasoning(result.proposal_id, session)
            try:
                from .data.memory_vectors import remember_proposal
                remember_proposal(
                    self.config, result.proposal_id, symbol=draft.symbol,
                    strategy_tag=draft.strategy_tag, thesis=draft.thesis,
                    status=result.status,
                )
            except Exception:
                pass
            if result.status == "submitted":
                report.submitted += 1
            elif result.status == "pending_approval":
                report.pending_approval += 1
            else:
                report.rejected += 1

    def _tag_discovery(self, draft: OrderProposal) -> None:
        """Stamp discovery_source + score_at_entry from the scanner candidate pool."""
        if getattr(draft, "discovery_source", None):
            return
        try:
            from .scanner.movers import load_candidates
            core = {s.upper() for s in self.config.settings.universe.core}
            for c in load_candidates(self.config):
                if str(c.get("symbol", "")).upper() == draft.symbol.upper():
                    draft.discovery_source = c.get("discovery_source") or "scanner"
                    draft.score_at_entry = float(c.get("score") or 0)
                    # Prefer scanner template as strategy_tag when still generic.
                    tmpl = c.get("template")
                    if tmpl and draft.strategy_tag in ("manual", "t", ""):
                        draft.strategy_tag = tmpl
                    return
            draft.discovery_source = "core" if draft.symbol.upper() in core else "scanner"
        except Exception:
            pass

    def _manage_positions(self, account, report: CycleReport, market_open: bool) -> None:
        """Deterministic exits (no LLM): stop/target/trailing/time/expiry rules from
        config.limits.exits. Stock exits are submitted through the guardrail pipeline;
        option exits are flagged for the strategy agent (a defined-risk close needs the
        multi-leg path, so we don't synthesize a naked closing leg here)."""
        # Drop peaks for names we no longer hold, before the early return below --
        # otherwise re-entering a symbol inherits the old run's high-water mark and
        # the trailing rule closes the new position on its first tick.
        try:
            held = [p.symbol for p in (account.positions or [])]
            self.journal.conn.execute(
                "DELETE FROM kv_state WHERE key LIKE 'hwm:%'"
                + (" AND key NOT IN (%s)" % ",".join("?" * len(held)) if held else ""),
                [f"hwm:{s}" for s in held],
            )
            self.journal.conn.commit()
        except Exception:
            pass

        if not account.positions:
            return
        from datetime import date

        from .analytics.exits import ExitRules, evaluate_exits
        from .execution_pricing import marketable_limit

        ex = self.config.limits.exits
        quotes, marks = {}, {}
        for p in account.positions:
            try:
                q = self.broker.get_quote(p.symbol)
                quotes[p.symbol], marks[p.symbol] = q, q.mid
            except Exception:
                continue
        opened: dict = {}
        try:
            for lot in self.journal.open_lots():
                sym, d = lot["symbol"], date.fromisoformat(lot["open_ts"][:10])
                if sym not in opened or d < opened[sym]:
                    opened[sym] = d
        except Exception:
            pass
        # High-water marks: the trailing rule's missing half. ExitRules documents
        # high_water as caller-owned, but nobody ever populated it, so trailing_pct
        # was inert whatever it was set to ("off until a high-water source is wired").
        # Persisted per symbol so a daemon restart does not reset the peak to entry
        # and hand back a whole run's profit.
        high_water: dict[str, float] = {}
        for p in account.positions:
            mark = marks.get(p.symbol)
            if mark is None or mark <= 0:
                continue
            key = f"hwm:{p.symbol}"
            try:
                prior = float(self.journal.get_state(key) or 0) or None
            except ValueError:
                prior = None
            # Peak favorable: the high for longs, the low for shorts.
            peak = mark if prior is None else (
                max(prior, mark) if p.qty > 0 else min(prior, mark))
            high_water[p.symbol] = peak
            if peak != prior:
                self.journal.set_state(key, str(peak))

        rules = ExitRules(
            stop_loss_pct=ex.stop_loss_pct, take_profit_pct=ex.take_profit_pct,
            trailing_pct=ex.trailing_pct, max_holding_days=ex.max_holding_days,
            option_roll_dte=ex.option_roll_dte, open_dates=opened,
            high_water=high_water,
        )
        for act in evaluate_exits(account.positions, marks, rules=rules):
            pos = account.position_for(act.symbol)
            if pos is None or pos.qty == 0:
                continue
            if pos.asset_class == "option":
                self._close_option(pos, act, account, report, market_open)
                continue
            q = quotes.get(act.symbol)
            if q is None:
                continue
            side = "sell" if pos.qty > 0 else "buy"
            proposal = OrderProposal(
                agent="exit_manager", strategy_tag="deterministic_exit",
                symbol=act.symbol, asset_class="stock", side=side, qty=abs(pos.qty),
                order_type="limit",
                limit_price=marketable_limit(side, *q.effective_book, aggressiveness=0.7),
                reduces_position=True, thesis=f"{act.action}: {act.reason}",
                expected_edge_usd=0.0,
            )
            try:
                res = self.pipeline.process(proposal, account, q, market_is_open=market_open)
                report.notes.append(f"exit {act.symbol} ({act.reason}): {res.status}")
            except Exception as e:
                report.notes.append(f"exit {act.symbol} failed: {e}")

    def _close_option(self, pos, act, account, report: CycleReport, market_open: bool) -> None:
        """Execute a defined-risk close of one option position through the guardrail
        pipeline: submit the opposite side of the held leg (sell to close a long, buy
        to close a short), reduces_position so the guardrail treats it as risk shed,
        not a naked open. Closing near expiry is the assignment defense — 'roll' and
        'close' both resolve to a close here; re-opening is a fresh agent decision."""
        from .broker.models import Quote
        from .broker.occ import parse_occ
        from .execution_pricing import marketable_limit
        from .guardrails.models import OptionLeg

        try:
            parts = parse_occ(pos.symbol)
        except ValueError:
            report.notes.append(f"exit skipped (unparseable OCC) {pos.symbol}: {act.reason}")
            return
        qty = abs(int(pos.qty))
        if qty <= 0:
            return
        side = "sell" if pos.qty > 0 else "buy"

        limit = None
        premium = abs(pos.market_value) / (qty * 100) if qty else 0.0  # mark fallback
        try:
            oq = self.broker.get_option_quote(pos.symbol)
            bid, ask = oq.effective_book
            if bid > 0 and ask > 0:
                limit = round(marketable_limit(side, bid, ask, aggressiveness=0.7), 2)
                premium = oq.mid
        except Exception:
            pass

        leg = OptionLeg(side=side, right=parts.right, strike=parts.strike,
                        expiry=parts.expiry, qty=qty, est_premium=max(premium, 0.0),
                        occ_symbol=pos.symbol)
        proposal = OrderProposal(
            agent="exit_manager", strategy_tag="deterministic_exit",
            symbol=parts.underlying, asset_class="option", side=side, legs=[leg],
            order_type="limit", limit_price=limit if limit and limit > 0 else None,
            reduces_position=True, thesis=f"{act.action}: {act.reason}",
            expected_edge_usd=0.0,
        )
        try:
            uq = self.broker.get_quote(parts.underlying)
        except Exception:
            uq = Quote(symbol=parts.underlying, bid=0.0, ask=0.0)
        try:
            res = self.pipeline.process(proposal, account, uq, market_is_open=market_open)
            report.notes.append(f"option exit {pos.symbol} ({act.reason}): {res.status}")
        except Exception as e:
            report.notes.append(f"option exit {pos.symbol} failed: {e}")

    def _risk_size(self, draft: OrderProposal, account, report: CycleReport,
                   *, regime: tuple = (None, None)) -> None:
        """Clamp an opening stock buy to a volatility-targeted size (never larger than
        the agent proposed), then scale by a drawdown throttle. Off when
        portfolio.vol_target_annual is 0. Best-effort: no vol/price -> leave as-is."""
        pl = self.config.limits.portfolio
        if pl.vol_target_annual <= 0:
            return
        if draft.asset_class != "stock" or draft.side != "buy" or draft.reduces_position:
            return
        vol = self._symbol_vol(draft.symbol)
        price = draft.limit_price or self._mark(draft.symbol)
        if not vol or vol <= 0 or not price or price <= 0 or account.equity <= 0:
            return
        from .analytics.sizing import drawdown_throttle, vol_target_size

        target = vol_target_size(
            account.equity, price, vol,
            target_annual_vol=pl.vol_target_annual,
            max_weight=self.config.limits.position.max_position_pct / 100.0,
        )
        dd = 0.0
        try:
            peak = float(self.journal.get_state("equity_peak", "0") or 0)
            if peak > 0:
                dd = max(0.0, (peak - account.equity) / peak)
        except Exception:
            pass
        circuit = (pl.drawdown_circuit_pct / 100.0) if pl.drawdown_circuit_pct > 0 else 0.15
        # Auto-calibrated per-strategy sizing multiplier (<=1.0): scale into what
        # the graded ledger shows works, out of what doesn't. Bounded in kv_state.
        from .analytics.autocalibrate import size_multiplier
        cal_mult = size_multiplier(self.journal, draft.strategy_tag)
        # Regime conditioning: the sweep measures each strategy against an
        # exposure-matched passive hold IN EACH REGIME, and these strategies lose in
        # strong uptrends while earning their keep in choppier tape. Scale risk into
        # the regimes where the edge was actually measured. 1.0 when the regime is
        # unknown or under-sampled -- absence of evidence must not shrink positions.
        from .analytics.sweep import regime_size_multiplier
        reg_mult = regime_size_multiplier(
            self.journal, draft.strategy_tag, regime[0], regime[1])
        sized = int(target * drawdown_throttle(dd, soft=circuit / 2, hard=circuit)
                    * cal_mult * reg_mult)
        if sized < draft.qty:
            old = draft.qty
            draft.qty = max(sized, 0)
            report.notes.append(
                f"vol-sized {draft.symbol} {old:g}->{draft.qty:g} "
                f"(vol {vol:.0%}"
                + (f", cal x{cal_mult:g}" if cal_mult < 1.0 else "")
                + (f", regime {regime[0]}/{regime[1]} x{reg_mult:.2f}"
                   if reg_mult < 1.0 else "")
                + (f", dd {dd:.0%}" if dd > 0 else "") + ")"
            )

    def _symbol_vol(self, symbol: str) -> float | None:
        """Latest captured realized (annualized) vol for a symbol from the per-interval
        signal snapshot dataset."""
        try:
            import json
            row = self.journal.conn.execute(
                "SELECT features_json FROM signal_snapshot WHERE symbol=? "
                "AND features_json IS NOT NULL ORDER BY id DESC LIMIT 1",
                (symbol.upper(),)).fetchone()
            if row and row["features_json"]:
                return json.loads(row["features_json"]).get("realized_vol")
        except Exception:
            pass
        return None

    def _mark(self, symbol: str) -> float | None:
        try:
            return self.broker.get_quote(symbol).mid
        except Exception:
            return None

    def _session_now(self) -> datetime:
        """Now in the configured market timezone.

        The day boundary must be the TRADING day. This used to use UTC midnight,
        which is 20:00 ET the previous evening — so an evening's spend counted
        against the next morning's budget.
        """
        try:
            from zoneinfo import ZoneInfo

            return datetime.now(ZoneInfo(self.config.settings.schedule.timezone))
        except Exception:
            return datetime.now(timezone.utc)

    def _cost_capped(self, *, reserve: float = _POSTCLOSE_RESERVE_USD,
                     pace: bool = True) -> bool:
        """True if Anthropic spend has hit the daily cap or this hour's paced slice.

        Three windows, because two were not enough:

        * Calendar day in MARKET time, not UTC and not trailing 24h. A trailing
          window let last night's spend suppress this morning; UTC midnight put the
          boundary at 20:00 ET the evening before.
        * A reserve the intraday agent cannot touch, so the postclose learning
          cycle is not starved by the cycle that generates its evidence.
        * A per-hour slice sized by how much of the SESSION IS LEFT, not a flat
          share. A flat 25% still let four hours eat a 6.5-hour day — on
          2026-07-29 the budget was gone by 13:00 ET and the system went dark
          through the FOMC decision.
        """
        cap = self.config.settings.agents.max_daily_cost_usd
        if cap <= 0:
            return False
        now = self._session_now()
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        spent = self.journal.cost_since(
            day_start.astimezone(timezone.utc).isoformat())

        usable = max(0.0, cap - max(0.0, reserve))
        if spent >= usable:
            detail = f"today ${spent:.2f} >= ${usable:.2f}"
            if reserve > 0:
                detail += f" (cap ${cap:.2f} less ${reserve:.2f} postclose reserve)"
            self.journal.heartbeat("cost_cap", status="warn", detail=detail)
            try:
                notify.notify_event(self.config, self.journal, "Cost cap reached",
                                    f"Anthropic spend today ${spent:.2f} >= "
                                    f"${usable:.2f}; agent cycles paused")
            except Exception:
                pass
            return True
        if not pace:
            return False

        # Spread what is left over what is left. Self-correcting: a quiet morning
        # widens the afternoon's allowance rather than expiring unused.
        hours_left = _SESSION_CLOSE_HOUR - (now.hour + now.minute / 60.0)
        hours_left = min(max(hours_left, 1.0),
                         float(_SESSION_CLOSE_HOUR - _SESSION_OPEN_HOUR))
        remaining = usable - spent
        hour_cap = min(remaining,
                       max(usable * _MIN_HOURLY_BUDGET_SHARE, remaining / hours_left))
        hour_spent = self.journal.cost_since(
            now.replace(minute=0, second=0, microsecond=0)
               .astimezone(timezone.utc).isoformat())
        if hour_spent >= hour_cap:
            # Deliberately quiet: this is pacing, not an incident. It clears on the
            # hour, so alerting on it would page once an hour for normal operation.
            self.journal.heartbeat(
                "cost_cap", status="warn",
                detail=f"hourly ${hour_spent:.2f} >= ${hour_cap:.2f} "
                       f"({hours_left:.1f}h left, paced)")
            return True
        return False

    def _billing_halted(self) -> bool:
        """True while we are backing off from a billing rejection.

        A dead credit balance is not a transient error -- retrying it once a minute
        cannot fix it. On 2026-07-28 that produced 102 identical 400s and 102 alerts
        between 18:16 and 19:59. Probe once every _BILLING_RETRY_MINUTES instead, so
        the system still recovers by itself when the balance is topped up."""
        stamp = self.journal.get_state(_BILLING_HALT_KEY)
        if not stamp:
            return False
        try:
            age = datetime.now(timezone.utc) - datetime.fromisoformat(stamp)
        except ValueError:
            return False
        return age < timedelta(minutes=_BILLING_RETRY_MINUTES)

    def _alert_llm_down(self, exc: Exception) -> None:
        """Loud alarm when the agent can no longer think. Credit exhaustion is
        called out by name: it silently blinded the system mid-session on 7/22,
        leaving an open position unmanaged for the rest of the day."""
        detail = str(exc)
        out_of_credit = "credit balance is too low" in detail.lower()
        title = "Anthropic credit exhausted" if out_of_credit else "Strategy agent down"
        self.journal.heartbeat("llm", status="error", detail=detail[:400])
        if out_of_credit:
            self.journal.set_state(_BILLING_HALT_KEY,
                                   datetime.now(timezone.utc).isoformat())
        try:
            notify.notify_event(
                self.config, self.journal, title,
                f"{detail[:300]}\nDeterministic exits still running; "
                "no new proposals until this is resolved.",
            )
        except Exception:
            pass

    def _record_usage(self, cycle: str, agent: str, usage, report: CycleReport,
                      model: str | None = None) -> None:
        # `model` must be whatever actually served the call. The risk/redteam agents
        # resolve their model without a cycle, so re-resolving here with one would
        # price an Opus review at Sonnet rates and under-meter it again.
        model = model or self.config.settings.agents.model_for(agent, cycle=cycle)
        cost_usd = cost.estimate_cost(usage, model)
        report.cost_usd += cost_usd
        self.journal.record_usage(
            cycle=cycle, agent=agent, model=model,
            input_tokens=usage.input_tokens, output_tokens=usage.output_tokens,
            cache_read_tokens=usage.cache_read_tokens, cost_usd=cost_usd,
            cache_write_tokens=getattr(usage, "cache_write_tokens", 0),
        )

    def _arm_plan(self, draft: OrderProposal, verdict) -> int:
        """Journal the approved-but-waiting order and store it for the tick stream.

        The proposal row is written with status 'armed' so the plan is auditable
        before it fires; the real submission gets its own proposal row at fire time,
        which is what keeps counterfactual grading honest about when we committed."""
        pid = self.journal.record_proposal(
            agent=draft.agent, strategy_tag=draft.strategy_tag, symbol=draft.symbol,
            asset_class=draft.asset_class, side=draft.side, qty=draft.qty,
            order_type=draft.order_type, limit_price=draft.limit_price,
            stop_price=draft.stop_price, target_price=draft.target_price,
            legs=[l.model_dump(mode="json") for l in draft.legs] or None,
            thesis=draft.thesis, expected_edge_usd=draft.expected_edge_usd,
            max_loss_usd=draft.max_loss_usd, confidence=draft.confidence,
            discovery_source=getattr(draft, "discovery_source", None),
            score_at_entry=getattr(draft, "score_at_entry", None),
        )
        self.journal.set_proposal_status(pid, "armed")
        self.journal.record_verdict(
            pid, source="risk_agent", verdict=verdict.verdict,
            reason=f"{verdict.reason} | armed at {draft.arm_direction} {draft.arm_level:g}",
        )
        expires = (datetime.now(timezone.utc)
                   + timedelta(hours=draft.arm_valid_hours)).isoformat()
        self.journal.arm_plan(
            symbol=draft.symbol, direction=draft.arm_direction, level=draft.arm_level,
            expires_at=expires, proposal_json=draft.model_dump_json(),
            note=f"proposal {pid}",
        )
        return pid

    def _journal_veto(self, draft: OrderProposal, source: str, verdict) -> int:
        pid = self.journal.record_proposal(
            agent=draft.agent, strategy_tag=draft.strategy_tag, symbol=draft.symbol,
            asset_class=draft.asset_class, side=draft.side, qty=draft.qty,
            order_type=draft.order_type, limit_price=draft.limit_price,
            stop_price=draft.stop_price, target_price=draft.target_price,
            legs=[l.model_dump(mode="json") for l in draft.legs] or None,
            thesis=draft.thesis, expected_edge_usd=draft.expected_edge_usd,
            max_loss_usd=draft.max_loss_usd, confidence=draft.confidence,
            discovery_source=getattr(draft, "discovery_source", None),
            score_at_entry=getattr(draft, "score_at_entry", None),
        )
        self.journal.record_verdict(
            pid, source=source, verdict="veto",
            reason=verdict.reason + (f" | concerns: {'; '.join(verdict.concerns)}"
                                     if verdict.concerns else ""),
        )
        self.journal.set_proposal_status(pid, "vetoed")
        try:
            from .data.memory_vectors import remember_proposal
            remember_proposal(
                self.config, pid, symbol=draft.symbol,
                strategy_tag=draft.strategy_tag, thesis=draft.thesis,
                status="vetoed",
            )
        except Exception:
            pass
        return pid

    def _record_reasoning(self, proposal_id: int, session) -> None:
        if getattr(session, "reasoning", "") or getattr(session, "tool_calls", None):
            self.journal.record_reasoning(
                proposal_id=proposal_id, agent="strategy",
                reasoning=session.reasoning, tool_calls=session.tool_calls,
            )

    def _would_be_day_trade(self, draft: OrderProposal) -> bool:
        if draft.side != "sell" or draft.asset_class != "stock":
            return False
        today = datetime.now(timezone.utc).date()
        for lot in self.journal.open_lots(draft.symbol):
            opened = datetime.fromisoformat(lot["open_ts"]).date()
            if opened == today:
                return True
        return False

    def _quote_for(self, draft: OrderProposal):
        from .broker.models import Quote

        if draft.asset_class == "option":
            # Underlying quote is sufficient for the guardrail cost/notional model.
            return self.broker.get_quote(draft.symbol)
        try:
            return self.broker.get_quote(draft.symbol)
        except Exception:
            price = draft.limit_price or 0.0
            return Quote(symbol=draft.symbol, bid=price, ask=price)

    # -- postclose: deterministic scoring + qualitative lessons --------------

    def _postclose_cycle(self, account, report: CycleReport) -> None:
        # 0. Keep tax basis current: apply any wash-sale deferrals before scoring.
        from .analytics.tax import apply_wash_sale_adjustments

        adj = apply_wash_sale_adjustments(
            self.journal, self.config.limits.wash_sale.window_days
        )
        if adj:
            report.notes.append(f"{len(adj)} wash-sale adjustment(s)")

        # 1. Deterministic numeric scoring of every closed-but-unscored trade.
        score_report = score_closed_trades(self.journal)
        report.notes.append(
            f"scored {score_report.scored} trades (gross ${score_report.gross_pnl:+.2f})"
        )

        # 1b. Counterfactual outcomes for aged vetoed/rejected proposals — grades
        # the analysis loop even when no trade was taken.
        graded = 0
        try:
            from .analytics.counterfactuals import evaluate_pending
            from .data.memory_vectors import remember_outcome

            cf = evaluate_pending(self.journal, self.broker)
            graded = cf.evaluated
            if cf.evaluated:
                report.notes.append(
                    f"counterfactuals: {cf.evaluated} graded "
                    f"(right={cf.right} wrong={cf.wrong})"
                )
                for row in self.journal.all_proposal_outcomes()[-cf.evaluated:]:
                    prop = self.journal.get_proposal(row["proposal_id"])
                    if prop:
                        remember_outcome(
                            self.config, row["proposal_id"],
                            symbol=prop["symbol"],
                            hyp_pnl=float(row["hypothetical_pnl"] or 0),
                            verdict_was_right=(
                                None if row["verdict_was_right"] is None
                                else bool(row["verdict_was_right"])
                            ),
                            notes=row.get("notes"),
                        )
        except Exception as e:
            report.notes.append(f"counterfactuals failed: {e}")

        # 2. Qualitative lessons from the scoring agent -> memory/lessons.md.
        #    Deterministic scoring above always runs; the LLM lessons pause under the
        #    cost cap.
        # Postclose spends the reserve intraday could not touch, and is not paced —
        # it is a once-a-day cycle, so an hourly slice is meaningless for it. This
        # is the learning loop; starving it is how the system stops improving.
        if self._cost_capped(reserve=0.0, pace=False):
            report.notes.append("scoring agent skipped (cost cap)")
        elif not score_report.scored and not graded:
            # No graded outcome and no closed trade means there is nothing to learn
            # from. Asked anyway, the agent writes lessons from recalled P&L — on
            # 7/22 it invented three day-move figures and got UNH's sign wrong.
            report.notes.append("scoring agent skipped (no graded evidence)")
        else:
            try:
                spent = cost.Usage()
                lessons = self._score(
                    self.client, self.config, self.journal, self.broker, account,
                    usage=spent,
                )
                self._record_usage("postclose", "scoring", spent, report)
                report.notes.append(f"{len(lessons)} lessons recorded")
            except Exception as e:
                report.notes.append(f"scoring agent failed: {e}")
        # 3. EOD performance snapshot into memory.
        perf = portfolio_summary(self.journal, self.config.settings.tax)
        today = f"Scored today: {score_report.scored} round-trip lot(s), " \
                f"gross ${score_report.gross_pnl:+.2f}"
        self._write_memory(
            "eod_review.md", "postclose",
            f"{today}\n\n{perf}\n\n{open_positions_summary(account)}"
            f"\n\nStages: {lifecycle.stages_summary(self.journal)}")
        report.notes.append("wrote memory/eod_review.md")

    # -- premarket: write a watchlist note -----------------------------------

    def _note_cycle(self, cycle: str, account, report: CycleReport) -> None:
        # Premarket runs once, before the session — nothing to pace against.
        if self._cost_capped(reserve=0.0, pace=False):
            report.notes.append("agent research skipped (cost cap)")
            return
        # Refresh the market-intel digest at the start of the day.
        if cycle == "premarket":
            try:
                spent = cost.Usage()
                if self._run_curation(spent):
                    report.notes.append("refreshed market-intel digest")
                self._record_usage(cycle, "intel", spent, report)
            except Exception as e:
                report.notes.append(f"intel curation failed: {e}")
        session = self._run_strategy(
            self.client, self.config, self.journal, self.broker, account, cycle=cycle
        )
        self._record_usage(cycle, "strategy", session.usage, report)
        # Fold overnight scanner leftovers into the premarket user context was already
        # available via tools; append candidate digest to the watchlist note.
        note = session.final_text
        if cycle == "premarket":
            try:
                from .scanner.movers import candidate_context, run_movers_scan
                # Fresh premarket scan so the watchlist agent + note see overnight movers.
                run_movers_scan(self.config, self.broker, journal=self.journal)
                cand = candidate_context(self.config)
                if cand:
                    note = f"{note}\n\n---\n{cand}"
            except Exception as e:
                report.notes.append(f"premarket movers failed: {e}")
        self._write_memory("watchlist.md", cycle, note)
        report.notes.append("wrote memory/watchlist.md")
        if cycle == "premarket":
            try:
                from .triggers import extract_and_save_from_watchlist
                triggers = extract_and_save_from_watchlist(self.config, note)
                report.notes.append(f"parsed {len(triggers)} intraday trigger(s)")
            except Exception as e:
                report.notes.append(f"trigger parse failed: {e}")

    def _intel_digest(self) -> str:
        import os

        from .data.intel import IntelStore

        path = self.config.settings.paths.intel_db
        if not os.path.exists(path):
            return ""
        store = IntelStore(path)
        try:
            d = store.latest_digest()
            return d["digest_md"] if d else ""
        finally:
            store.close()

    def _run_curation(self, usage=None) -> str:
        import os

        from .data.intel import IntelStore

        path = self.config.settings.paths.intel_db
        if not os.path.exists(path) or self.client is None:
            return ""
        store = IntelStore(path)
        try:
            return intel_mod.run_intel_session(self.client, self.config, store, usage)
        finally:
            store.close()

    # -- weekend: weekly rollup + playbook research --------------------------

    def _weekend_cycle(self, account, report: CycleReport) -> None:
        from .analytics.scorer import run_weekly

        weekly = run_weekly(self.journal, self.config)
        for ch in weekly.changes:
            report.notes.append(f"lifecycle: {ch.tag} {ch.old_stage}->{ch.new_stage}")

        # Re-allocate capital across strategies by risk-adjusted after-tax expectancy.
        from .analytics.allocation import allocate_capital, persist_allocations

        allocations = allocate_capital(self.journal, self.config.settings.tax)
        persist_allocations(self.journal, allocations)
        top = ", ".join(f"{a.tag} {a.weight*100:.0f}%" for a in allocations[:3] if a.weight > 0)
        report.notes.append(f"allocation: {top or 'no positive-expectancy strategies'}")

        # Confidence calibration + veto-quality — the back-analysis of the analysis.
        calibration_text = ""
        try:
            from .analytics.calibration import (
                build_calibration_report, persist_calibration,
            )
            calib = build_calibration_report(self.journal)
            persist_calibration(
                self.journal, calib, self.config.settings.paths.memory_dir
            )
            calibration_text = calib.text
            report.notes.append(
                f"calibration: outcomes={calib.n_outcomes} "
                f"veto_hit={calib.veto_hit_rate}"
            )
        except Exception as e:
            report.notes.append(f"calibration failed: {e}")

        # Sentiment → forward-return studies (no fills required).
        signal_text = ""
        try:
            from .analytics.signal_research_runner import (
                persist_signal_research, run_signal_research,
            )
            sig = run_signal_research(self.config, self.broker)
            persist_signal_research(self.config, sig)
            signal_text = sig.text
            report.notes.append(
                f"signal_research: {len(sig.studies)} studies, "
                f"{len(sig.promising)} promising"
            )
        except Exception as e:
            report.notes.append(f"signal_research failed: {e}")

        # Deterministic weekly rollup + allocation above always run; the research
        # agent pauses under the cost cap.
        # Weekend: no session to pace across, and no intraday competing for it.
        if self._cost_capped(reserve=0.0, pace=False):
            report.notes.append("weekend research agent skipped (cost cap)")
            return
        extra_parts = []
        if calibration_text:
            extra_parts.append(
                "Weekly calibration report (use this to propose playbook edits):\n"
                + calibration_text
            )
        if signal_text:
            extra_parts.append(
                "Sentiment→return signal research (promising = candidate for backtest):\n"
                + signal_text
            )
        extra = ("\n\n" + "\n\n".join(extra_parts)) if extra_parts else ""
        session = self._run_strategy(
            self.client, self.config, self.journal, self.broker, account,
            cycle="weekend", extra_context=extra,
        )
        self._record_usage("weekend", "research", session.usage, report)
        self._write_memory("weekend_research.md", "weekend", session.final_text)

        # Paper mode: auto-apply structured playbook edits from the research note.
        try:
            from .analytics.playbooks import apply_playbook_edits
            pb = apply_playbook_edits(
                session.final_text,
                playbooks_dir=self.config.settings.paths.playbooks_dir,
                memory_dir=self.config.settings.paths.memory_dir,
                paper_mode=not self.config.is_live,
            )
            report.notes.append(f"playbook edits: {pb.summary()}")
            if pb.applied:
                self.journal.heartbeat(
                    "playbooks", status="ok",
                    detail="; ".join(pb.applied[:5]),
                )
        except Exception as e:
            report.notes.append(f"playbook apply failed: {e}")

        report.notes.append(f"weekly: {len(weekly.changes)} stage changes; "
                            "wrote memory/weekend_research.md")

    def _write_memory(self, fname: str, cycle: str, body: str) -> None:
        mem_dir = Path(self.config.settings.paths.memory_dir)
        mem_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).isoformat(timespec="minutes")
        (mem_dir / fname).write_text(
            f"# {cycle} note — {stamp}\n\n{body}\n", encoding="utf-8"
        )

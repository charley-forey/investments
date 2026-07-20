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
from .analytics.stats import portfolio_summary
from .broker.sync import cancel_stale_orders, sync_fills
from .config import Config
from .data.journal import Journal
from .guardrails.engine import OrderPipeline
from .guardrails.models import OrderProposal


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
        if self.skipped:
            return f"[{self.cycle}] skipped: {self.skipped}"
        return (f"[{self.cycle}] proposals={self.proposals} vetoed={self.vetoed} "
                f"submitted={self.submitted} rejected={self.rejected} "
                f"pending={self.pending_approval} cost=${self.cost_usd:.3f}")


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
            if self._cost_capped():
                report.skipped = "cost cap reached"
                self.journal.heartbeat(f"cycle:{cycle}", status="skip", detail=report.skipped)
                return report
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

        stages = lifecycle.stages_summary(self.journal)
        perf = portfolio_summary(self.journal, self.config.settings.tax)
        extra = f"Strategy stages: {stages}\n{perf}"
        digest = self._intel_digest()
        if digest:
            extra += f"\n\nMarket intelligence digest:\n{digest}"
        session = self._run_strategy(
            self.client, self.config, self.journal, self.broker, account,
            cycle="intraday", extra_context=extra,
        )
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

        # Deterministic per-interval signal snapshot of the whole universe (no LLM).
        try:
            from .analytics.snapshot import snapshot_universe
            snapshot_universe(self.config, self.journal, self.broker, cycle="intraday")
        except Exception as e:
            report.notes.append(f"snapshot failed: {e}")

        for draft in session.drafts:
            would_be_dt = self._would_be_day_trade(draft)
            verdict = self._review(
                self.client, self.config, self.journal, self.broker, account, draft
            )
            if verdict.verdict != "approve":
                report.vetoed += 1
                pid = self._journal_veto(draft, "risk_agent", verdict)
                self._record_reasoning(pid, session)
                continue

            # High-conviction trades get an adversarial red-team pass; a veto here
            # skips the trade even though risk approved it.
            if redteam_mod.should_red_team(self.config, draft):
                rt = self._red_team(
                    self.client, self.config, self.journal, self.broker, account, draft
                )
                if rt.verdict != "approve":
                    report.vetoed += 1
                    pid = self._journal_veto(draft, "redteam", rt)
                    self._record_reasoning(pid, session)
                    continue

            # Approved by risk -> run the deterministic guardrail pipeline.
            quote = self._quote_for(draft)
            result = self.pipeline.process(
                draft, account, quote,
                market_is_open=market_open, would_be_day_trade=would_be_dt,
            )
            # Attach the risk approval + captured strategy reasoning to the proposal.
            self.journal.record_verdict(
                result.proposal_id, source="risk_agent", verdict="approve",
                reason=verdict.reason,
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

    def _manage_positions(self, account, report: CycleReport, market_open: bool) -> None:
        """Deterministic exits (no LLM): stop/target/trailing/time/expiry rules from
        config.limits.exits. Stock exits are submitted through the guardrail pipeline;
        option exits are flagged for the strategy agent (a defined-risk close needs the
        multi-leg path, so we don't synthesize a naked closing leg here)."""
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
        rules = ExitRules(
            stop_loss_pct=ex.stop_loss_pct, take_profit_pct=ex.take_profit_pct,
            trailing_pct=ex.trailing_pct, max_holding_days=ex.max_holding_days,
            option_roll_dte=ex.option_roll_dte, open_dates=opened,
        )
        for act in evaluate_exits(account.positions, marks, rules=rules):
            pos = account.position_for(act.symbol)
            if pos is None or pos.qty == 0:
                continue
            if pos.asset_class != "stock":
                report.notes.append(f"exit flagged (option) {act.symbol}: {act.reason}")
                continue
            q = quotes.get(act.symbol)
            if q is None:
                continue
            side = "sell" if pos.qty > 0 else "buy"
            proposal = OrderProposal(
                agent="exit_manager", strategy_tag="deterministic_exit",
                symbol=act.symbol, asset_class="stock", side=side, qty=abs(pos.qty),
                order_type="limit",
                limit_price=marketable_limit(side, q.bid, q.ask, aggressiveness=0.7),
                reduces_position=True, thesis=f"{act.action}: {act.reason}",
                expected_edge_usd=0.0,
            )
            try:
                res = self.pipeline.process(proposal, account, q, market_is_open=market_open)
                report.notes.append(f"exit {act.symbol} ({act.reason}): {res.status}")
            except Exception as e:
                report.notes.append(f"exit {act.symbol} failed: {e}")

    def _cost_capped(self) -> bool:
        """True if the trailing-24h Anthropic spend has hit the configured cap —
        runaway-cost protection that pauses agent (LLM) work."""
        cap = self.config.settings.agents.max_daily_cost_usd
        if cap <= 0:
            return False
        day_ago = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        spent = self.journal.cost_since(day_ago)
        if spent >= cap:
            self.journal.heartbeat("cost_cap", status="warn",
                                   detail=f"24h spend ${spent:.2f} >= cap ${cap:.2f}")
            try:
                notify.notify_event(self.config, self.journal, "Cost cap reached",
                                    f"24h Anthropic spend ${spent:.2f} >= ${cap:.2f}; "
                                    "agent cycles paused")
            except Exception:
                pass
            return True
        return False

    def _record_usage(self, cycle: str, agent: str, usage, report: CycleReport) -> None:
        model = self.config.settings.agents.model
        cost_usd = cost.estimate_cost(usage, model)
        report.cost_usd += cost_usd
        self.journal.record_usage(
            cycle=cycle, agent=agent, model=model,
            input_tokens=usage.input_tokens, output_tokens=usage.output_tokens,
            cache_read_tokens=usage.cache_read_tokens, cost_usd=cost_usd,
        )

    def _journal_veto(self, draft: OrderProposal, source: str, verdict) -> int:
        pid = self.journal.record_proposal(
            agent=draft.agent, strategy_tag=draft.strategy_tag, symbol=draft.symbol,
            asset_class=draft.asset_class, side=draft.side, qty=draft.qty,
            order_type=draft.order_type, limit_price=draft.limit_price,
            stop_price=draft.stop_price,
            legs=[l.model_dump(mode="json") for l in draft.legs] or None,
            thesis=draft.thesis, expected_edge_usd=draft.expected_edge_usd,
            max_loss_usd=draft.max_loss_usd, confidence=draft.confidence,
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
        try:
            from .analytics.counterfactuals import evaluate_pending
            from .data.memory_vectors import remember_outcome

            cf = evaluate_pending(self.journal, self.broker)
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
        if self._cost_capped():
            report.notes.append("scoring agent skipped (cost cap)")
        else:
            try:
                lessons = self._score(
                    self.client, self.config, self.journal, self.broker, account
                )
                report.notes.append(f"{len(lessons)} lessons recorded")
            except Exception as e:
                report.notes.append(f"scoring agent failed: {e}")
        # 3. EOD performance snapshot into memory.
        perf = portfolio_summary(self.journal, self.config.settings.tax)
        self._write_memory("eod_review.md", "postclose",
                           f"{perf}\n\nStages: {lifecycle.stages_summary(self.journal)}")
        report.notes.append("wrote memory/eod_review.md")

    # -- premarket: write a watchlist note -----------------------------------

    def _note_cycle(self, cycle: str, account, report: CycleReport) -> None:
        if self._cost_capped():
            report.notes.append("agent research skipped (cost cap)")
            return
        # Refresh the market-intel digest at the start of the day.
        if cycle == "premarket":
            try:
                if self._run_curation():
                    report.notes.append("refreshed market-intel digest")
            except Exception as e:
                report.notes.append(f"intel curation failed: {e}")
        session = self._run_strategy(
            self.client, self.config, self.journal, self.broker, account, cycle=cycle
        )
        self._record_usage(cycle, "strategy", session.usage, report)
        self._write_memory("watchlist.md", cycle, session.final_text)
        report.notes.append("wrote memory/watchlist.md")

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

    def _run_curation(self) -> str:
        import os

        from .data.intel import IntelStore

        path = self.config.settings.paths.intel_db
        if not os.path.exists(path) or self.client is None:
            return ""
        store = IntelStore(path)
        try:
            return intel_mod.run_intel_session(self.client, self.config, store)
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
        if self._cost_capped():
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

"""Orchestrator: one cycle = snapshot -> sync -> strategy agent -> risk review ->
guardrail pipeline. This is the wiring that turns agents into (paper) trades.

Dependency injection on the constructor keeps it fully testable with scripted
agents and a stub broker.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import cost, notify
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
            try:
                if not self.broker.market_open():
                    report.skipped = "market closed"
                    self.journal.heartbeat(f"cycle:{cycle}", status="skip", detail=report.skipped)
                    return report
            except Exception as e:
                report.notes.append(f"market check failed, proceeding: {e}")

        account = self.broker.get_account_state(self.journal)

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

        stages = lifecycle.stages_summary(self.journal)
        perf = portfolio_summary(self.journal, self.config.settings.tax)
        extra = f"Strategy stages: {stages}\n{perf}"
        session = self._run_strategy(
            self.client, self.config, self.journal, self.broker, account,
            cycle="intraday", extra_context=extra,
        )
        self._record_usage("intraday", "strategy", session.usage, report)
        report.proposals = len(session.drafts)

        market_open = True
        try:
            market_open = self.broker.market_open()
        except Exception:
            pass

        for draft in session.drafts:
            would_be_dt = self._would_be_day_trade(draft)
            verdict = self._review(
                self.client, self.config, self.journal, self.broker, account, draft
            )
            if verdict.verdict != "approve":
                report.vetoed += 1
                self._journal_veto(draft, "risk_agent", verdict)
                continue

            # High-conviction trades get an adversarial red-team pass; a veto here
            # skips the trade even though risk approved it.
            if redteam_mod.should_red_team(self.config, draft):
                rt = self._red_team(
                    self.client, self.config, self.journal, self.broker, account, draft
                )
                if rt.verdict != "approve":
                    report.vetoed += 1
                    self._journal_veto(draft, "redteam", rt)
                    continue

            # Approved by risk -> run the deterministic guardrail pipeline.
            quote = self._quote_for(draft)
            result = self.pipeline.process(
                draft, account, quote,
                market_is_open=market_open, would_be_day_trade=would_be_dt,
            )
            # Attach the risk approval to the same proposal record.
            self.journal.record_verdict(
                result.proposal_id, source="risk_agent", verdict="approve",
                reason=verdict.reason,
            )
            if result.status == "submitted":
                report.submitted += 1
            elif result.status == "pending_approval":
                report.pending_approval += 1
            else:
                report.rejected += 1

    def _record_usage(self, cycle: str, agent: str, usage, report: CycleReport) -> None:
        model = self.config.settings.agents.model
        cost_usd = cost.estimate_cost(usage, model)
        report.cost_usd += cost_usd
        self.journal.record_usage(
            cycle=cycle, agent=agent, model=model,
            input_tokens=usage.input_tokens, output_tokens=usage.output_tokens,
            cache_read_tokens=usage.cache_read_tokens, cost_usd=cost_usd,
        )

    def _journal_veto(self, draft: OrderProposal, source: str, verdict) -> None:
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
        # 2. Qualitative lessons from the scoring agent -> memory/lessons.md.
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
        session = self._run_strategy(
            self.client, self.config, self.journal, self.broker, account, cycle=cycle
        )
        self._record_usage(cycle, "strategy", session.usage, report)
        self._write_memory("watchlist.md", cycle, session.final_text)
        report.notes.append("wrote memory/watchlist.md")

    # -- weekend: weekly rollup + playbook research --------------------------

    def _weekend_cycle(self, account, report: CycleReport) -> None:
        from .analytics.scorer import run_weekly

        weekly = run_weekly(self.journal, self.config)
        for ch in weekly.changes:
            report.notes.append(f"lifecycle: {ch.tag} {ch.old_stage}->{ch.new_stage}")
        session = self._run_strategy(
            self.client, self.config, self.journal, self.broker, account, cycle="weekend"
        )
        self._record_usage("weekend", "research", session.usage, report)
        self._write_memory("weekend_research.md", "weekend", session.final_text)
        report.notes.append(f"weekly: {len(weekly.changes)} stage changes; "
                            "wrote memory/weekend_research.md")

    def _write_memory(self, fname: str, cycle: str, body: str) -> None:
        mem_dir = Path(self.config.settings.paths.memory_dir)
        mem_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).isoformat(timespec="minutes")
        (mem_dir / fname).write_text(
            f"# {cycle} note — {stamp}\n\n{body}\n", encoding="utf-8"
        )

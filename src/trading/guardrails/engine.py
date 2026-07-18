"""The guardrail engine and order pipeline — the ONLY path to the broker.

evaluate() is pure validation over (proposal, account state, quote, journal
history). OrderPipeline journals everything, then either rejects, queues for
human approval (live), or submits (paper / approved live).
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone

from ..broker.models import AccountState, Quote
from ..config import Config
from ..data.journal import Journal
from . import account_math
from .models import GuardrailResult, OrderProposal, PipelineResult, Violation

# Common leveraged/inverse ETFs blocked unless allow_leveraged_etfs is set.
LEVERAGED_ETFS = {
    "TQQQ", "SQQQ", "UPRO", "SPXU", "SPXL", "SPXS", "SOXL", "SOXS",
    "TNA", "TZA", "UVXY", "SVXY", "LABU", "LABD", "FAS", "FAZ", "UDOW", "SDOW",
}


class GuardrailEngine:
    def __init__(self, config: Config, journal: Journal):
        self.config = config
        self.journal = journal

    def evaluate(
        self,
        proposal: OrderProposal,
        account: AccountState,
        quote: Quote,
        *,
        market_is_open: bool | None = None,
        would_be_day_trade: bool = False,
    ) -> GuardrailResult:
        limits = self.config.limits
        violations: list[Violation] = []

        def fail(rule: str, message: str) -> None:
            violations.append(Violation(rule=rule, message=message))

        # 0. Kill switch — check first, and trip it if today's loss breaches the cap.
        if (
            account.daily_pl_pct <= -limits.loss_kill_switch.max_daily_loss_pct
            and not self.journal.kill_switch_active()
        ):
            self.journal.trip_kill_switch(
                f"daily loss {account.daily_pl_pct:.2f}% breached "
                f"-{limits.loss_kill_switch.max_daily_loss_pct}% limit"
            )
        if self.journal.kill_switch_active() and not proposal.reduces_position:
            fail(
                "kill_switch",
                f"kill switch active ({self.journal.get_state('kill_switch_reason')}); "
                "only position-reducing orders allowed until manual reset",
            )

        # 1. Market hours.
        if market_is_open is False:
            fail("market_hours", "market is closed")

        # 2. Order type.
        if proposal.order_type == "market" and not limits.orders.allow_market_orders:
            fail("order_type", "market orders are disabled (allow_market_orders=false)")

        # 3. Symbol rules.
        sym = proposal.symbol
        if sym in {s.upper() for s in limits.symbols.blocklist}:
            fail("symbol_blocklist", f"{sym} is blocklisted")
        if sym in LEVERAGED_ETFS and not limits.symbols.allow_leveraged_etfs:
            fail("leveraged_etf", f"{sym} is a leveraged/inverse ETF (not allowed)")
        ref_price = quote.mid
        if 0 < ref_price < limits.symbols.min_price and not proposal.is_option:
            fail("min_price", f"{sym} at ${ref_price:.2f} is below min ${limits.symbols.min_price:.2f}")

        # 4. Trade-budget counters (opening trades only).
        if not proposal.reduces_position:
            now = datetime.now(timezone.utc)
            day_start = datetime.combine(now.date(), time.min, tzinfo=timezone.utc)
            week_start = day_start - timedelta(days=now.weekday())
            if self.journal.trades_since(day_start) >= limits.orders.max_new_trades_per_day:
                fail("daily_trade_budget",
                     f"daily trade budget ({limits.orders.max_new_trades_per_day}) exhausted")
            if self.journal.trades_since(week_start) >= limits.orders.max_new_trades_per_week:
                fail("weekly_trade_budget",
                     f"weekly trade budget ({limits.orders.max_new_trades_per_week}) exhausted")

        # 5. PDT rule.
        if (
            limits.pdt.enforce
            and would_be_day_trade
            and account.equity < limits.pdt.equity_threshold_usd
        ):
            day_trades = max(account.daytrade_count, self.journal.day_trades_last_n_days(5))
            if day_trades >= limits.pdt.max_day_trades_per_5_days:
                fail(
                    "pdt",
                    f"would be day trade #{day_trades + 1} in 5 days with equity "
                    f"${account.equity:,.0f} < ${limits.pdt.equity_threshold_usd:,.0f}",
                )

        # 6. Wash sale (buying back after a recent realized loss).
        if limits.wash_sale.enforce and proposal.side == "buy" and not proposal.reduces_position:
            loss = self.journal.last_realized_loss(sym, limits.wash_sale.window_days)
            if loss is not None:
                fail(
                    "wash_sale",
                    f"realized loss of ${loss['realized_pnl']:,.2f} in {sym} on "
                    f"{loss['close_ts'][:10]}; re-buy blocked for "
                    f"{limits.wash_sale.window_days} days",
                )

        # 7. Notional / position caps.
        notional = account_math.proposal_notional_usd(proposal, quote)
        if notional <= 0:
            fail("notional", "order notional computes to zero — missing price/qty")
        if notional > limits.orders.max_order_notional_usd:
            fail("max_order_notional",
                 f"notional ${notional:,.2f} > cap ${limits.orders.max_order_notional_usd:,.2f}")

        pos_cap = min(
            limits.position.max_position_usd,
            account.equity * limits.position.max_position_pct / 100.0,
        )
        existing = account.position_for(sym)
        existing_value = abs(existing.market_value) if existing else 0.0
        if not proposal.reduces_position and existing_value + notional > pos_cap:
            fail(
                "max_position",
                f"position would be ${existing_value + notional:,.2f} > cap ${pos_cap:,.2f} "
                f"(min of ${limits.position.max_position_usd:,.2f} and "
                f"{limits.position.max_position_pct}% of equity)",
            )
        if (
            not proposal.reduces_position
            and existing is None
            and account.open_position_count >= limits.position.max_open_positions
        ):
            fail("max_open_positions",
                 f"already at max open positions ({limits.position.max_open_positions})")

        # 8. Options rules — recompute risk independently from the legs.
        computed_max_loss: float | None = None
        if proposal.is_option:
            if not proposal.legs:
                fail("options_legs", "option proposal has no legs")
            else:
                shares_held = existing.qty if existing and existing.asset_class == "stock" else 0.0
                analysis = account_math.analyze_option_legs(
                    proposal.legs,
                    underlying_shares_held=shares_held,
                    cash_available=account.cash,
                )
                computed_max_loss = analysis.max_loss_usd
                if limits.options.defined_risk_only and not analysis.is_defined_risk:
                    fail("naked_option", "; ".join(analysis.notes) or "undefined-risk structure")
                if analysis.max_loss_usd > limits.options.max_loss_per_trade_usd:
                    fail(
                        "options_max_loss",
                        f"computed max loss ${analysis.max_loss_usd:,.2f} > cap "
                        f"${limits.options.max_loss_per_trade_usd:,.2f}",
                    )
                total_contracts = sum(l.qty for l in proposal.legs)
                if total_contracts > limits.options.max_contracts_per_order:
                    fail("options_contracts",
                         f"{total_contracts} contracts > cap {limits.options.max_contracts_per_order}")
                min_expiry = min(l.expiry for l in proposal.legs)
                dte = (min_expiry - date.today()).days
                if dte < limits.options.min_days_to_expiry:
                    fail("options_dte",
                         f"{dte} days to expiry < min {limits.options.min_days_to_expiry}")

        # 9. Cost hurdle — expected edge must clear estimated friction.
        est_cost = account_math.estimate_cost_usd(proposal, quote, limits.cost_hurdle)
        if limits.cost_hurdle.enforce and not proposal.reduces_position:
            if proposal.expected_edge_usd is None:
                fail("cost_hurdle", "no expected_edge_usd stated; cannot clear cost hurdle")
            elif proposal.expected_edge_usd < limits.cost_hurdle.min_edge_multiple * est_cost:
                fail(
                    "cost_hurdle",
                    f"expected edge ${proposal.expected_edge_usd:,.2f} < "
                    f"{limits.cost_hurdle.min_edge_multiple}x estimated cost ${est_cost:,.2f}",
                )

        return GuardrailResult(
            approved=not violations,
            violations=violations,
            est_cost_usd=round(est_cost, 4),
            computed_max_loss_usd=computed_max_loss,
            notional_usd=round(notional, 2),
        )


class OrderPipeline:
    """Journal -> validate -> (reject | queue for approval | submit)."""

    def __init__(self, config: Config, journal: Journal, broker=None):
        self.config = config
        self.journal = journal
        self.broker = broker
        self.engine = GuardrailEngine(config, journal)

    def process(
        self,
        proposal: OrderProposal,
        account: AccountState,
        quote: Quote,
        *,
        market_is_open: bool | None = None,
        would_be_day_trade: bool = False,
    ) -> PipelineResult:
        pid = self.journal.record_proposal(
            agent=proposal.agent,
            strategy_tag=proposal.strategy_tag,
            symbol=proposal.symbol,
            asset_class=proposal.asset_class,
            side=proposal.side,
            qty=proposal.qty,
            order_type=proposal.order_type,
            limit_price=proposal.limit_price,
            stop_price=proposal.stop_price,
            legs=[l.model_dump(mode="json") for l in proposal.legs] or None,
            thesis=proposal.thesis,
            expected_edge_usd=proposal.expected_edge_usd,
            max_loss_usd=proposal.max_loss_usd,
            confidence=proposal.confidence,
        )

        result = self.engine.evaluate(
            proposal, account, quote,
            market_is_open=market_is_open,
            would_be_day_trade=would_be_day_trade,
        )

        if not result.approved:
            for v in result.violations:
                self.journal.record_verdict(
                    pid, source="guardrail", verdict="reject", rule=v.rule, reason=v.message
                )
            self.journal.set_proposal_status(pid, "rejected")
            return PipelineResult(proposal_id=pid, status="rejected", result=result)

        self.journal.record_verdict(pid, source="guardrail", verdict="approve")

        # Live approval gate: auto_submit_below_usd=0 means every live order
        # needs human approval; otherwise only orders at/above the threshold do.
        if self.config.is_live and self.config.limits.live.approval_required:
            threshold = self.config.limits.live.auto_submit_below_usd
            notional = result.notional_usd or 0.0
            if threshold <= 0 or notional >= threshold:
                self.journal.set_proposal_status(pid, "pending_approval")
                return PipelineResult(
                    proposal_id=pid, status="pending_approval", result=result
                )

        return self._submit(pid, proposal, result)

    def approve(self, proposal_id: int) -> PipelineResult:
        """Human approval of a pending live order (called from the CLI)."""
        row = self.journal.get_proposal(proposal_id)
        if row is None or row["status"] != "pending_approval":
            raise ValueError(f"proposal {proposal_id} is not pending approval")
        self.journal.record_verdict(proposal_id, source="human", verdict="approve")
        proposal = OrderProposal(
            agent=row["agent"], strategy_tag=row["strategy_tag"], symbol=row["symbol"],
            asset_class=row["asset_class"], side=row["side"], qty=row["qty"],
            order_type=row["order_type"], limit_price=row["limit_price"],
        )
        result = GuardrailResult(approved=True, notional_usd=None)
        return self._submit(proposal_id, proposal, result)

    def _submit(
        self, pid: int, proposal: OrderProposal, result: GuardrailResult
    ) -> PipelineResult:
        if proposal.is_option:
            # Multi-leg option submission lands in milestone 4; validation already works.
            self.journal.record_verdict(
                pid, source="guardrail", verdict="reject", rule="options_execution",
                reason="option order execution not yet enabled (milestone 4)",
            )
            self.journal.set_proposal_status(pid, "rejected")
            result = result.model_copy(update={
                "approved": False,
                "violations": result.violations + [Violation(
                    rule="options_execution",
                    message="option order execution not yet enabled (milestone 4)",
                )],
            })
            return PipelineResult(proposal_id=pid, status="rejected", result=result)

        broker_order_id = None
        if self.broker is not None:
            broker_order_id = self.broker.submit_order(
                symbol=proposal.symbol,
                side=proposal.side,
                qty=proposal.qty,
                order_type=proposal.order_type,
                limit_price=proposal.limit_price,
            )
        order_id = self.journal.record_order(
            proposal_id=pid,
            mode=self.config.limits.mode,
            symbol=proposal.symbol,
            side=proposal.side,
            qty=proposal.qty,
            order_type=proposal.order_type,
            limit_price=proposal.limit_price,
            broker_order_id=broker_order_id,
        )
        self.journal.set_proposal_status(pid, "submitted")
        return PipelineResult(
            proposal_id=pid, status="submitted", result=result,
            order_id=order_id, broker_order_id=broker_order_id,
        )

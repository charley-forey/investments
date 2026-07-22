"""The guardrail engine and order pipeline — the ONLY path to the broker.

evaluate() is pure validation over (proposal, account state, quote, journal
history). OrderPipeline journals everything, then either rejects, queues for
human approval (live), or submits (paper / approved live).
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone

from ..analytics import lifecycle
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


def _count_underlying(account: AccountState, underlying: str) -> int:
    """How many current positions share this underlying (options resolve their
    OCC symbol back to the underlying)."""
    from ..broker.occ import parse_occ

    underlying = underlying.upper()
    n = 0
    for p in account.positions:
        u = p.symbol.upper()
        try:
            u = parse_occ(p.symbol).underlying
        except ValueError:
            pass
        if u == underlying:
            n += 1
    return n


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
        option_leg_spreads: dict[str, float] | None = None,
        regime_gross_scale: float = 1.0,
        max_holding_correlation: float | None = None,
    ) -> GuardrailResult:
        limits = self.config.limits
        violations: list[Violation] = []

        def fail(rule: str, message: str) -> None:
            violations.append(Violation(rule=rule, message=message))

        # Portfolio drawdown circuit breaker: track the equity high-water mark and
        # trip the kill switch on a peak-to-trough decline past the threshold. This
        # catches slow bleeds that the single-day loss kill switch misses.
        dd_pct = limits.portfolio.drawdown_circuit_pct
        if dd_pct > 0 and account.equity > 0:
            peak = float(self.journal.get_state("equity_peak", "0") or 0)
            peak = max(peak, account.equity)
            self.journal.set_state("equity_peak", str(peak))
            if peak > 0 and 100.0 * (peak - account.equity) / peak > dd_pct:
                if not self.journal.kill_switch_active():
                    self.journal.trip_kill_switch(
                        f"drawdown {(peak - account.equity) / peak * 100:.1f}% from peak "
                        f"${peak:,.0f} breached {dd_pct}% circuit breaker")

        # Reconciliation halt: a broker/journal position mismatch blocks new
        # entries (position-reducing orders are still allowed).
        if not proposal.reduces_position and self.journal.get_state("reconcile_halt"):
            fail("reconcile_halt",
                 f"position reconciliation mismatch — new entries blocked until resolved "
                 f"({self.journal.get_state('reconcile_halt')})")

        # Strategy lifecycle stage. A candidate/backtest tag (sizing fraction 0)
        # cannot trade in any mode. In live mode only small-live/scaled may trade,
        # and the stage's fraction scales the position cap.
        stage = lifecycle.get_stage(self.journal, proposal.strategy_tag)
        stage_frac = lifecycle.STAGE_SIZING.get(stage, 0.0)
        live_size_scale = 1.0
        if not proposal.reduces_position:
            if stage_frac <= 0:
                fail("strategy_stage",
                     f"strategy '{proposal.strategy_tag}' at stage '{stage}' is not "
                     f"cleared to trade (needs a passing backtest to reach paper)")
            elif self.config.is_live:
                # Stage fraction x the human-approved live-scaling ladder multiplier.
                from ..analytics import scaling

                live_size_scale = stage_frac * scaling.multiplier(self.journal)
                if stage not in ("small-live", "scaled"):
                    fail("strategy_stage",
                         f"strategy '{proposal.strategy_tag}' at stage '{stage}' is not "
                         f"cleared for live trading (needs small-live or scaled)")

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

        # 6. Wash sale (buying back after a recent realized loss). In "defer" mode
        # the re-buy is allowed and the tax module adjusts basis instead of blocking.
        if (limits.wash_sale.enforce and limits.wash_sale.mode == "block"
                and proposal.side == "buy" and not proposal.reduces_position):
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
        ) * live_size_scale
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

        # 7b. Portfolio-level risk: gross exposure (regime-scaled), per-underlying
        # concentration, and return-correlation concentration.
        if not proposal.reduces_position:
            gross = sum(abs(p.market_value) for p in account.positions) + notional
            gross_cap = (account.equity * limits.portfolio.max_gross_exposure_pct / 100.0
                         * regime_gross_scale)
            if account.equity > 0 and gross > gross_cap:
                scale_note = "" if regime_gross_scale >= 1.0 else \
                    f" (tightened x{regime_gross_scale} for regime)"
                fail("max_gross_exposure",
                     f"gross exposure ${gross:,.2f} > cap ${gross_cap:,.2f}"
                     f"{scale_note}")

            per_underlying = _count_underlying(account, sym)
            if existing is None and per_underlying >= limits.portfolio.max_positions_per_underlying:
                fail("max_per_underlying",
                     f"already {per_underlying} position(s) in {sym}; cap is "
                     f"{limits.portfolio.max_positions_per_underlying}")

            corr_cap = limits.portfolio.max_position_correlation
            if (max_holding_correlation is not None and corr_cap < 1.0
                    and existing is None and max_holding_correlation > corr_cap):
                fail("correlation_concentration",
                     f"return correlation {max_holding_correlation:.2f} to an existing "
                     f"holding exceeds cap {corr_cap:.2f} — too correlated to add as a "
                     f"separate position")

            # 7c. Net directional delta cap: limit how net-long/short the whole book
            # can get (a market-neutral book can run high gross but low net). Stock
            # proposals add signed delta; defined-risk options are capped separately.
            net_cap_pct = limits.portfolio.max_net_delta_pct
            if net_cap_pct > 0 and account.equity > 0:
                from ..analytics.portfolio_risk import portfolio_risk
                book = [{"symbol": p.symbol, "qty": p.qty,
                         "price": (abs(p.market_value / p.qty) if p.qty else p.avg_entry_price),
                         "asset_class": p.asset_class} for p in account.positions]
                if not proposal.is_option:
                    signed = proposal.qty if proposal.side == "buy" else -proposal.qty
                    book.append({"symbol": sym, "qty": signed, "price": ref_price,
                                 "asset_class": "stock"})
                net_delta = portfolio_risk(book, account.equity).net_delta_dollars
                delta_cap = account.equity * net_cap_pct / 100.0
                if abs(net_delta) > delta_cap:
                    fail("max_net_delta",
                         f"net delta ${net_delta:,.0f} would exceed cap ${delta_cap:,.0f} "
                         f"({net_cap_pct:.0f}% of equity)")

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
        est_cost = account_math.estimate_cost_usd(
            proposal, quote, limits.cost_hurdle, option_leg_spreads=option_leg_spreads
        )
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
            discovery_source=getattr(proposal, "discovery_source", None),
            score_at_entry=getattr(proposal, "score_at_entry", None),
        )

        option_leg_spreads = self._option_leg_spreads(proposal)
        regime_scale, holding_corr = self._portfolio_context(proposal, account)
        result = self.engine.evaluate(
            proposal, account, quote,
            market_is_open=market_is_open,
            would_be_day_trade=would_be_day_trade,
            option_leg_spreads=option_leg_spreads,
            regime_gross_scale=regime_scale,
            max_holding_correlation=holding_corr,
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
                self._notify_pending(pid, proposal, result)
                return PipelineResult(
                    proposal_id=pid, status="pending_approval", result=result
                )

        return self._submit(pid, proposal, result)

    def _portfolio_context(self, proposal, account) -> tuple[float, float | None]:
        """Best-effort regime gross-scale and correlation-to-holdings. Both degrade
        to no-ops (scale 1.0, corr None) if data or the bar store isn't available —
        so this never blocks a decision on missing data, and never creates files."""
        import os

        regime_scale = 1.0
        holding_corr = None
        if self.broker is None:
            return regime_scale, holding_corr

        # Regime: tighten gross exposure in an elevated-volatility tape.
        try:
            from ..tools.market_context import market_regime

            regime = market_regime(self.broker)
            if regime.vol_state == "elevated":
                regime_scale = self.config.limits.portfolio.elevated_vol_gross_scale
        except Exception:
            pass

        # Correlation: only if the bar store already exists (never create it here).
        try:
            corr_cap = self.config.limits.portfolio.max_position_correlation
            bars_db = self.config.settings.paths.bars_db
            holdings = [p.symbol for p in account.positions if account.position_for(p.symbol)]
            if (corr_cap < 1.0 and not proposal.reduces_position and holdings
                    and os.path.exists(bars_db)):
                from ..analytics.risk import max_correlation_to_holdings
                from ..data.bars import BarStore

                store = BarStore(bars_db)
                cand = [b.close for b in store.load_bars(proposal.symbol)]
                if cand:
                    holdings_closes = {
                        s: [b.close for b in store.load_bars(s)]
                        for s in holdings if s != proposal.symbol
                    }
                    holdings_closes = {s: c for s, c in holdings_closes.items() if c}
                    if holdings_closes:
                        holding_corr = max_correlation_to_holdings(
                            cand, holdings_closes).max_correlation
                store.close()
        except Exception:
            pass

        return regime_scale, holding_corr

    def _option_leg_spreads(self, proposal: OrderProposal) -> dict[str, float] | None:
        """Best-effort real per-leg option spreads for the cost hurdle. Falls back
        to the premium approximation (returns None) if quotes aren't available."""
        if not proposal.is_option or self.broker is None:
            return None
        from ..broker.occ import build_occ

        get_q = getattr(self.broker, "get_option_quote", None)
        if get_q is None:
            return None
        spreads: dict[str, float] = {}
        for leg in proposal.legs:
            occ = leg.occ_symbol or build_occ(
                proposal.symbol, leg.expiry, leg.right, leg.strike
            )
            try:
                spreads[occ] = get_q(occ).spread
            except Exception:
                return None  # incomplete data -> use the conservative approximation
        return spreads

    def _notify_pending(self, pid: int, proposal: OrderProposal, result: GuardrailResult) -> None:
        """Async approval ping (Discord). Best-effort; never blocks the pipeline."""
        try:
            from .. import notify

            desc = (f"{proposal.side} {proposal.qty:g} {proposal.symbol} "
                    f"({proposal.asset_class}, {proposal.strategy_tag})")
            if proposal.is_option:
                desc = "; ".join(f"{l.side} {l.qty} {l.right} {l.strike} {l.expiry}"
                                 for l in proposal.legs)
            msg = (f"**LIVE ORDER PENDING APPROVAL** #{pid}\n{desc}\n"
                   f"notional ${result.notional_usd or 0:,.2f}\n"
                   f"thesis: {proposal.thesis or 'n/a'}\n"
                   f"Approve with: `trading approve {pid}`")
            notify.send(self.config, msg)
        except Exception:
            pass

    def approve(self, proposal_id: int) -> PipelineResult:
        """Human approval of a pending live order (called from the CLI)."""
        import json

        from .models import OptionLeg

        row = self.journal.get_proposal(proposal_id)
        if row is None or row["status"] != "pending_approval":
            raise ValueError(f"proposal {proposal_id} is not pending approval")
        self.journal.record_verdict(proposal_id, source="human", verdict="approve")
        legs = [OptionLeg(**leg) for leg in json.loads(row["legs_json"])] if row["legs_json"] else []
        proposal = OrderProposal(
            agent=row["agent"], strategy_tag=row["strategy_tag"], symbol=row["symbol"],
            asset_class=row["asset_class"], side=row["side"], qty=row["qty"],
            order_type=row["order_type"], limit_price=row["limit_price"], legs=legs,
        )
        result = GuardrailResult(approved=True, notional_usd=None)
        return self._submit(proposal_id, proposal, result)

    def _submit(
        self, pid: int, proposal: OrderProposal, result: GuardrailResult
    ) -> PipelineResult:
        if proposal.is_option:
            return self._submit_option(pid, proposal, result)

        # Protective bracket: opening long positions with a stop attach a stop-loss
        # (and a take-profit at N x risk if no explicit target) atomically.
        stop_loss = take_profit = None
        if not proposal.reduces_position and proposal.side == "buy" and proposal.stop_price:
            stop_loss = proposal.stop_price
            take_profit = proposal.target_price
            if take_profit is None and proposal.limit_price:
                risk = proposal.limit_price - proposal.stop_price
                r_mult = self.config.limits.orders.bracket_default_target_r
                if risk > 0 and r_mult > 0:
                    take_profit = round(proposal.limit_price + r_mult * risk, 2)

        broker_order_id = None
        if self.broker is not None:
            broker_order_id = self.broker.submit_order(
                symbol=proposal.symbol,
                side=proposal.side,
                qty=proposal.qty,
                order_type=proposal.order_type,
                limit_price=proposal.limit_price,
                stop_loss_price=stop_loss,
                take_profit_price=take_profit,
                client_order_id=f"prop-{pid}",  # idempotent + exact attribution
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

    def _submit_option(
        self, pid: int, proposal: OrderProposal, result: GuardrailResult
    ) -> PipelineResult:
        from ..broker.occ import build_occ

        legs = []
        for leg in proposal.legs:
            occ = leg.occ_symbol or build_occ(
                proposal.symbol, leg.expiry, leg.right, leg.strike
            )
            legs.append({"occ_symbol": occ, "side": leg.side, "qty": leg.qty})

        # Net limit price: explicit proposal limit, else net debit from est premiums
        # (buys pay, sells receive).
        net_limit = proposal.limit_price
        if net_limit is None:
            net = sum((1 if l.side == "buy" else -1) * l.est_premium for l in proposal.legs)
            net_limit = abs(net)

        broker_order_id = None
        if self.broker is not None:
            broker_order_id = self.broker.submit_option_order(
                legs=legs, net_limit_price=net_limit, underlying=proposal.symbol,
                client_order_id=f"prop-{pid}",
            )
        total_contracts = sum(l.qty for l in proposal.legs)
        order_id = self.journal.record_order(
            proposal_id=pid,
            mode=self.config.limits.mode,
            symbol=proposal.symbol,
            side=proposal.side,
            qty=total_contracts,
            order_type="limit",
            limit_price=net_limit,
            broker_order_id=broker_order_id,
        )
        self.journal.set_proposal_status(pid, "submitted")
        return PipelineResult(
            proposal_id=pid, status="submitted", result=result,
            order_id=order_id, broker_order_id=broker_order_id,
        )

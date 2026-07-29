"""Regressions for the 2026-07-28 day review.

That session spent $13.47 across 72 strategy LLM sessions to place one trade, ran
the Anthropic balance to zero at 14:16 ET and then retried a fatal 400 once a minute
for 103 minutes, disabled its own exit management the moment the cost cap tripped,
and reported "7 trades" for what was a single SMCI position. One test per defect.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from trading.analytics.stats import compute_stats
from trading.config import TaxRates
from trading.data.journal import Journal
from trading.triggers import _novel_wake_events


def _journal(tmp_path) -> Journal:
    return Journal(tmp_path / "j.db")


class TestWakeEventCooldown:
    """475 ORB events on 7/28, drained 20 per cycle by a once-a-minute poller with
    no cooldown -> 72 back-to-back LLM sessions."""

    def test_repeat_symbol_kind_is_suppressed(self, tmp_path):
        j = _journal(tmp_path)
        events = [{"id": 1, "symbol": "NVDA", "kind": "orb", "price": 196.0}]
        assert len(_novel_wake_events(j, events)) == 1, "first event must wake"
        assert _novel_wake_events(j, events) == [], "repeat inside cooldown must not"

    def test_different_kind_still_wakes(self, tmp_path):
        j = _journal(tmp_path)
        _novel_wake_events(j, [{"id": 1, "symbol": "NVDA", "kind": "orb", "price": 196.0}])
        fresh = _novel_wake_events(
            j, [{"id": 2, "symbol": "NVDA", "kind": "trigger", "price": 196.0}])
        assert len(fresh) == 1

    def test_material_move_breaks_the_cooldown(self, tmp_path):
        """A breakout that keeps extending is new information; one that sits is not."""
        j = _journal(tmp_path)
        base = [{"id": 1, "symbol": "NVDA", "kind": "orb", "price": 200.0}]
        _novel_wake_events(j, base)
        drift = [{"id": 2, "symbol": "NVDA", "kind": "orb", "price": 200.5}]  # +0.25%
        assert _novel_wake_events(j, drift) == []
        run = [{"id": 3, "symbol": "NVDA", "kind": "orb", "price": 203.0}]  # +1.5%
        assert len(_novel_wake_events(j, run)) == 1

    def test_cooldown_expires(self, tmp_path):
        j = _journal(tmp_path)
        now = datetime.now(timezone.utc)
        ev = [{"id": 1, "symbol": "NVDA", "kind": "orb", "price": 196.0}]
        _novel_wake_events(j, ev, now=now)
        later = now + timedelta(minutes=25)
        assert len(_novel_wake_events(j, ev, now=later)) == 1

    def test_a_burst_of_distinct_symbols_all_wake(self, tmp_path):
        """Coalescing must not swallow a genuine broad move: 29 names gapping at
        13:35 is one session, but all 29 belong in its reason string."""
        j = _journal(tmp_path)
        burst = [{"id": i, "symbol": f"S{i}", "kind": "orb", "price": 10.0}
                 for i in range(29)]
        assert len(_novel_wake_events(j, burst)) == 29


class TestTradesSinceCountsOrdersNotRows:
    """sync_fills writes one `orders` row per incremental fill delta on top of the
    submission row. Counting rows burned 4 of 10 daily trades on one CRM entry."""

    def test_one_entry_with_three_partial_fills_is_one_trade(self, tmp_path):
        j = _journal(tmp_path)
        bid = "broker-abc"
        for qty in (49, 34, 9, 6):  # submission + three fill deltas
            j.record_order(proposal_id=1, mode="paper", symbol="CRM", side="buy",
                           qty=qty, order_type="limit", limit_price=179.7,
                           broker_order_id=bid)
        since = datetime.now(timezone.utc) - timedelta(days=1)
        assert j.trades_since(since) == 1

    def test_distinct_broker_orders_still_counted_separately(self, tmp_path):
        j = _journal(tmp_path)
        for i, sym in enumerate(["CRM", "NET"]):
            j.record_order(proposal_id=i, mode="paper", symbol=sym, side="buy",
                           qty=10, order_type="limit", limit_price=1.0,
                           broker_order_id=f"b{i}")
        since = datetime.now(timezone.utc) - timedelta(days=1)
        assert j.trades_since(since) == 2

    def test_rows_without_a_broker_id_are_not_collapsed(self, tmp_path):
        """Two unsubmitted rows are two distinct things, not one NULL group."""
        j = _journal(tmp_path)
        for i in range(2):
            j.record_order(proposal_id=i, mode="paper", symbol="CRM", side="buy",
                           qty=10, order_type="limit", limit_price=1.0,
                           broker_order_id=None)
        since = datetime.now(timezone.utc) - timedelta(days=1)
        assert j.trades_since(since) == 2


class TestRoundTripStats:
    """`trades` gates paper_to_live_min_trades (30). Counting tax lots meant one
    position exited in 30 partial fills would clear the bar for real money."""

    RATES = TaxRates(federal_short_term_rate=0.0, federal_long_term_rate=0.0,
                     state_rate=0.0)

    def _smci(self):
        # The real 7/28 data: 7 closed lots, one SMCI position, proposal 13.
        pnls = [-0.2155, -0.1079, -0.1080, -0.1081, -3.0127, -3.70, -9.5761]
        return [{"id": i, "proposal_id": 13, "strategy_tag": "news-impulse",
                 "pnl_usd": p, "notes": f"SMCI lot#{i} [term=short]"}
                for i, p in enumerate(pnls)]

    def test_seven_lots_from_one_proposal_are_one_trade(self):
        s = compute_stats(self._smci(), self.RATES)
        assert s.trades == 1
        assert s.gross_pnl == round(s.gross_pnl, 4)
        assert abs(s.gross_pnl - (-16.8283)) < 0.01, "P&L must be unchanged"
        assert abs(s.expectancy - (-16.8283)) < 0.01, "expectancy is now per position"

    def test_win_rate_is_per_position(self):
        """Lot-level counting can also invent wins: a position that netted a loss
        may contain individually-profitable lots."""
        scores = [
            {"id": 1, "proposal_id": 7, "strategy_tag": "t", "pnl_usd": 5.0, "notes": ""},
            {"id": 2, "proposal_id": 7, "strategy_tag": "t", "pnl_usd": -20.0, "notes": ""},
        ]
        s = compute_stats(scores, self.RATES)
        assert s.trades == 1
        assert s.wins == 0 and s.win_rate == 0.0
        assert s.gross_pnl == -15.0

    def test_separate_proposals_stay_separate(self):
        scores = [
            {"id": 1, "proposal_id": 1, "strategy_tag": "t", "pnl_usd": 10.0, "notes": ""},
            {"id": 2, "proposal_id": 2, "strategy_tag": "t", "pnl_usd": -4.0, "notes": ""},
        ]
        s = compute_stats(scores, self.RATES)
        assert s.trades == 2 and s.wins == 1

    def test_unattributed_lots_are_not_merged(self):
        """No proposal_id means unattributable; merging them would undercount."""
        scores = [
            {"id": 1, "proposal_id": None, "strategy_tag": "t", "pnl_usd": 1.0, "notes": ""},
            {"id": 2, "proposal_id": None, "strategy_tag": "t", "pnl_usd": 2.0, "notes": ""},
        ]
        assert compute_stats(scores, self.RATES).trades == 2


class TestEventWallGate:
    """The premarket note said "no swing long that must survive FOMC 7/29" and
    nothing enforced it. The risk agent named the FOMC in its verdict and its only
    lever was qty_mult=0.60, so CRM opened into the print anyway."""

    def _setup(self, tmp_path, days=2):
        import json

        from conftest import make_config
        from trading.config import EventGate

        cal = tmp_path / "calendar.json"
        tomorrow = (datetime.now(timezone.utc) + timedelta(days=1)).date().isoformat()
        cal.write_text(json.dumps([
            {"date": tomorrow, "symbol": "", "event": "FOMC", "event_type": "macro"},
        ]), encoding="utf-8")
        cfg = make_config(events=EventGate(block_stock_entry_within_days=days))
        cfg = cfg.model_copy(update={"settings": cfg.settings.model_copy(
            update={"paths": cfg.settings.paths.model_copy(
                update={"calendar_file": str(cal)})})})
        return cfg

    def _pipeline(self, cfg, tmp_path):
        from trading.guardrails.engine import OrderPipeline
        return OrderPipeline(cfg, Journal(tmp_path / "g.db"), broker=None)

    def test_stock_entry_into_a_print_is_rejected(self, tmp_path):
        from conftest import make_account, make_quote
        from test_guardrails import base_proposal

        p = self._pipeline(self._setup(tmp_path), tmp_path)
        res = p.process(base_proposal(), make_account(), make_quote(), market_is_open=True)
        assert res.status == "rejected"
        assert "event_wall" in {v.rule for v in res.result.violations}

    def test_exits_are_never_blocked(self, tmp_path):
        """A gate that can trap a position is worse than no gate."""
        from conftest import make_account, make_quote
        from test_guardrails import base_proposal

        p = self._pipeline(self._setup(tmp_path), tmp_path)
        res = p.process(base_proposal(side="sell", reduces_position=True),
                        make_account(), make_quote(), market_is_open=True)
        assert "event_wall" not in {v.rule for v in res.result.violations}

    def test_gate_off_by_default(self, tmp_path):
        from conftest import make_account, make_config, make_quote
        from test_guardrails import base_proposal

        p = self._pipeline(make_config(), tmp_path)
        res = p.process(base_proposal(), make_account(), make_quote(), market_is_open=True)
        assert "event_wall" not in {v.rule for v in res.result.violations}

    def test_far_horizon_does_not_trip(self, tmp_path):
        """days=0 disables; a print outside the window is not this trade's problem."""
        from conftest import make_account, make_quote
        from test_guardrails import base_proposal

        p = self._pipeline(self._setup(tmp_path, days=0), tmp_path)
        res = p.process(base_proposal(), make_account(), make_quote(), market_is_open=True)
        assert "event_wall" not in {v.rule for v in res.result.violations}


class TestTrailingReplacesTheTarget:
    """A resting take-profit and a trailing stop cannot coexist: the target is
    always nearer, so it always fires first. 1,280 backtested trades show
    "trail + target" scoring identically to "target" alone."""

    def _run(self, tmp_path, trailing):
        from conftest import make_account, make_config, make_quote
        from stubs import StubBroker
        from test_guardrails import base_proposal
        from trading.config import ExitLimits
        from trading.guardrails.engine import OrderPipeline

        cfg = make_config(exits=ExitLimits(trailing_pct=trailing))
        broker = StubBroker(make_account())
        p = OrderPipeline(cfg, Journal(tmp_path / f"t{trailing}.db"), broker=broker)
        p.process(base_proposal(stop_price=95.0, target_price=115.0),
                  make_account(), make_quote(), market_is_open=True)
        return broker.submitted[-1]

    def test_no_take_profit_leg_when_trailing_is_on(self, tmp_path):
        sent = self._run(tmp_path, 8.0)
        assert sent["stop_loss_price"] == 95.0, "the stop must always rest at the broker"
        assert sent["take_profit_price"] is None

    def test_take_profit_still_attached_when_trailing_is_off(self, tmp_path):
        sent = self._run(tmp_path, None)
        assert sent["stop_loss_price"] == 95.0
        assert sent["take_profit_price"] == 115.0


class TestExitsSurviveTheCostCap:
    """The cap sat on run_cycle ahead of _trade_cycle, so tripping it also disabled
    stop/target/trailing/expiry management and stale-order cancellation. It tripped
    at 10:30am ET on 7/28 and fired 208 times that day."""

    def _orch(self, tmp_path, positions=()):
        from conftest import make_config
        from stubs import StubBroker, make_account
        from trading.agents.runner import AgentResult
        from trading.orchestrator import Orchestrator

        cfg = make_config()
        j = Journal(tmp_path / "cap.db")
        acct = make_account()
        acct.positions = list(positions)
        broker = StubBroker(acct, market_open=True)

        def strategy_runner(client, c, jn, b, a, cycle="intraday", extra_context=""):
            raise AssertionError("the strategy LLM must not run under the cost cap")

        orch = Orchestrator(cfg, j, broker, client=None,
                            strategy_runner=strategy_runner,
                            risk_reviewer=lambda *a: None)
        return orch, j

    def test_manage_positions_runs_even_when_capped(self, tmp_path, monkeypatch):
        orch, j = self._orch(tmp_path)
        monkeypatch.setattr(orch, "_cost_capped", lambda: True)
        called = []
        monkeypatch.setattr(orch, "_manage_positions",
                            lambda *a, **k: called.append(True))
        report = orch.run_cycle("intraday")
        assert called, "exit management must run before the cost gate"
        assert report.skipped == "cost cap reached"

    def test_billing_halt_also_spares_exits(self, tmp_path, monkeypatch):
        orch, j = self._orch(tmp_path)
        j.set_state("llm_billing_halt", datetime.now(timezone.utc).isoformat())
        called = []
        monkeypatch.setattr(orch, "_manage_positions",
                            lambda *a, **k: called.append(True))
        report = orch.run_cycle("intraday")
        assert called
        assert "billing halt" in (report.skipped or "")


class TestBillingCircuitBreaker:
    """103 minutes of once-a-minute retries against a 400 that could never succeed."""

    def _orch(self, tmp_path):
        from conftest import make_config
        from stubs import StubBroker, make_account
        from trading.orchestrator import Orchestrator

        return Orchestrator(make_config(), Journal(tmp_path / "b.db"),
                            StubBroker(make_account()), client=None,
                            risk_reviewer=lambda *a: None)

    def test_credit_exhaustion_sets_a_halt(self, tmp_path):
        orch = self._orch(tmp_path)
        orch._alert_llm_down(RuntimeError(
            "Error code: 400 - Your credit balance is too low to access the "
            "Anthropic API."))
        assert orch._billing_halted() is True

    def test_other_failures_do_not_halt(self, tmp_path):
        """A timeout is transient — halting on it would stop trading for 30 minutes
        over a blip."""
        orch = self._orch(tmp_path)
        orch._alert_llm_down(RuntimeError("Connection reset by peer"))
        assert orch._billing_halted() is False

    def test_halt_expires_so_it_self_heals(self, tmp_path):
        orch = self._orch(tmp_path)
        stale = (datetime.now(timezone.utc) - timedelta(minutes=45)).isoformat()
        orch.journal.set_state("llm_billing_halt", stale)
        assert orch._billing_halted() is False, "must probe again once topped up"


class TestHighWaterMarks:
    """ExitRules.high_water is caller-owned and nobody populated it, so trailing_pct
    was inert at any value."""

    def _orch_with(self, tmp_path, symbol, qty, mark):
        from conftest import make_config
        from stubs import StubBroker, make_account
        from trading.broker.models import Quote
        from trading.config import ExitLimits
        from trading.orchestrator import CycleReport, Orchestrator

        acct = make_account()

        class P:
            def __init__(s):
                s.symbol, s.qty, s.avg_entry_price = symbol, qty, 100.0
                s.asset_class, s.unrealized_pl, s.market_value = "stock", 0.0, 0.0
        acct.positions = [P()]
        broker = StubBroker(acct, quotes={symbol: Quote(
            symbol=symbol, bid=mark - 0.05, ask=mark + 0.05)})
        cfg = make_config(exits=ExitLimits(trailing_pct=None, stop_loss_pct=None))
        orch = Orchestrator(cfg, Journal(tmp_path / "h.db"), broker, client=None,
                            risk_reviewer=lambda *a: None)
        orch._manage_positions(acct, CycleReport(cycle="intraday"), True)
        return orch, acct

    def test_peak_is_recorded_and_ratchets_up_only(self, tmp_path):
        orch, acct = self._orch_with(tmp_path, "CRM", 49, 181.0)
        assert float(orch.journal.get_state("hwm:CRM")) == 181.0
        from trading.broker.models import Quote
        from trading.orchestrator import CycleReport
        orch.broker._quotes["CRM"] = Quote(symbol="CRM", bid=178.95, ask=179.05)
        orch._manage_positions(acct, CycleReport(cycle="intraday"), True)
        assert float(orch.journal.get_state("hwm:CRM")) == 181.0, "peak must not fall"

    def test_peak_is_cleared_when_the_position_closes(self, tmp_path):
        """Otherwise a re-entry inherits the old run's peak and exits instantly."""
        from trading.orchestrator import CycleReport
        orch, acct = self._orch_with(tmp_path, "CRM", 49, 181.0)
        assert orch.journal.get_state("hwm:CRM") is not None
        acct.positions = []
        orch._manage_positions(acct, CycleReport(cycle="intraday"), True)
        assert not orch.journal.get_state("hwm:CRM")

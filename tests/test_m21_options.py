"""M21: options intelligence — enriched chain (greeks/IV), chain signals, IV rank,
per-interval capture, and a guardrail regression confirming the defined-risk toolkit
still holds (naked stays blocked)."""

from datetime import date, timedelta

from conftest import make_config
from stubs import StubBroker, make_account

from trading.analytics.options import chain_rows, chain_signals, contract_row, iv_rank
from trading.analytics.snapshot import snapshot_universe
from trading.broker.models import Quote
from trading.broker.occ import build_occ
from trading.data.journal import Journal
from trading.guardrails.account_math import analyze_option_legs
from trading.guardrails.models import OptionLeg
from trading.tools.registry import STRATEGY_TOOLS, ToolContext, ToolRegistry


# -- stub option snapshot objects (shape of Alpaca's OptionsSnapshot) ---------

class _NS:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def _snap(bid, ask, iv=None, delta=None, theta=None, size=None):
    greeks = _NS(delta=delta, gamma=0.01, theta=theta, vega=0.1) if delta is not None else None
    return _NS(latest_quote=_NS(bid_price=bid, ask_price=ask),
               greeks=greeks, implied_volatility=iv,
               latest_trade=_NS(size=size) if size is not None else None)


def _chain(spot=100.0, expiry=None):
    expiry = expiry or (date.today() + timedelta(days=30))
    call = build_occ("AAPL", expiry, "call", 100.0)
    put = build_occ("AAPL", expiry, "put", 100.0)
    return {
        call: _snap(2.0, 2.2, iv=0.30, delta=0.52, theta=-0.05, size=10),
        put: _snap(1.8, 2.0, iv=0.36, delta=-0.48, theta=-0.04, size=7),
    }


class OptionBroker(StubBroker):
    def __init__(self, chain, spot=100.0):
        super().__init__(make_account())
        self._chain = chain
        self._spot = spot

    def get_quote(self, symbol):
        return Quote(symbol=symbol.upper(), bid=self._spot - 0.05, ask=self._spot + 0.05)

    def get_options_chain(self, underlying):
        return self._chain


class TestContractRow:
    def test_greeks_and_iv_surfaced(self):
        chain = _chain()
        occ, snap = next(iter(chain.items()))
        row = contract_row(occ, snap, "AAPL", 100.0)
        assert row.iv == 0.30 and row.delta == 0.52 and row.theta == -0.05
        assert row.spread_bps is not None and row.spread_bps > 0

    def test_missing_greeks_degrade(self):
        occ = build_occ("AAPL", date.today() + timedelta(days=30), "call", 100.0)
        row = contract_row(occ, _snap(1.0, 1.1), "AAPL", 100.0)  # no iv/greeks
        assert row.delta is None and row.iv is None
        assert "—" in row.line()


class TestChainSignals:
    def test_put_skew_and_richness(self):
        rows = chain_rows(_chain(), "AAPL", 100.0)
        sig = chain_signals(rows, realized_vol=0.20)  # ATM IV ~0.33 > RV 0.20 => rich
        assert sig.richness == "rich"
        assert sig.pc_skew is not None and sig.pc_skew > 0  # put IV (.36) > call IV (.30)
        assert sig.n_contracts == 2

    def test_unknown_without_realized_vol(self):
        rows = chain_rows(_chain(), "AAPL", 100.0)
        assert chain_signals(rows, realized_vol=None).richness == "unknown"


class TestIVRank:
    def test_percentile(self):
        hist = [0.20, 0.25, 0.30, 0.35, 0.40]
        assert iv_rank(0.30, hist) == 60.0   # 3 of 5 <= 0.30
        assert iv_rank(0.45, hist) == 100.0
        assert iv_rank(0.30, [0.2, 0.3]) is None  # insufficient history


class TestChainTool:
    def test_tool_output_has_greeks(self, tmp_path):
        config = make_config()
        journal = Journal(tmp_path / "j.db")
        ctx = ToolContext(config=config, journal=journal, broker=OptionBroker(_chain()),
                          account_state=make_account(), agent_name="strategy")
        reg = ToolRegistry(ctx, STRATEGY_TOOLS)
        out = reg.dispatch("get_options_chain", {"symbol": "AAPL"})
        assert "iv" in out and "delta" in out
        assert "put/call skew" in out  # chain-level signal line


class TestSnapshotCapture:
    def test_snapshot_records_atm_iv(self, tmp_path):
        config = make_config()
        config.settings.universe.core = ["AAPL"]
        journal = Journal(tmp_path / "j.db")
        snapshot_universe(config, journal, OptionBroker(_chain()))
        row = journal.recent_snapshots()[0]
        assert row["symbol"] == "AAPL"
        assert row["atm_iv"] is not None
        assert row["pc_skew"] is not None and row["pc_skew"] > 0


class TestGuardrailRegression:
    """The full enabled toolkit stays defined-risk; naked stays blocked."""

    exp = date.today() + timedelta(days=30)

    def leg(self, side, right, strike, prem=1.0, qty=1):
        return OptionLeg(side=side, right=right, strike=strike, expiry=self.exp,
                         qty=qty, est_premium=prem)

    def test_debit_vertical_defined(self):
        legs = [self.leg("buy", "call", 100, 3.0), self.leg("sell", "call", 105, 1.0)]
        assert analyze_option_legs(legs).is_defined_risk

    def test_credit_vertical_defined(self):
        legs = [self.leg("sell", "put", 100, 3.0), self.leg("buy", "put", 95, 1.0)]
        assert analyze_option_legs(legs).is_defined_risk

    def test_cash_secured_put_defined(self):
        legs = [self.leg("sell", "put", 100, 2.0)]
        a = analyze_option_legs(legs, cash_available=100 * 100)
        assert a.is_defined_risk

    def test_covered_call_defined(self):
        legs = [self.leg("sell", "call", 105, 2.0)]
        a = analyze_option_legs(legs, underlying_shares_held=100)
        assert a.is_defined_risk

    def test_naked_short_call_blocked(self):
        legs = [self.leg("sell", "call", 105, 2.0)]  # no shares
        assert not analyze_option_legs(legs).is_defined_risk


# -- defined-risk vertical constructor + one-call propose tool ----------------

def _multi_chain(spot=100.0, expiry=None):
    """A 5-strike chain (90..110) of calls and puts around spot, mids that fall
    away from the money — enough to actually build a vertical."""
    expiry = expiry or (date.today() + timedelta(days=30))
    call_mid = {90: 11.0, 95: 7.0, 100: 3.5, 105: 1.5, 110: 0.6}
    put_mid = {90: 0.6, 95: 1.5, 100: 3.0, 105: 6.0, 110: 10.5}
    chain = {}
    for strike, mid in call_mid.items():
        chain[build_occ("AAPL", expiry, "call", strike)] = _snap(mid - 0.05, mid + 0.05, iv=0.30, delta=0.5)
    for strike, mid in put_mid.items():
        chain[build_occ("AAPL", expiry, "put", strike)] = _snap(mid - 0.05, mid + 0.05, iv=0.34, delta=-0.5)
    return chain


def _rows(spot=100.0):
    from trading.analytics.options import chain_rows
    return chain_rows(_multi_chain(spot), "AAPL", spot, max_dte=60, moneyness_band=0.15)


class TestBuildVertical:
    def test_bullish_debit_call_vertical_sized_to_cap(self):
        from trading.analytics.options import build_vertical
        plan, note = build_vertical(_rows(), direction="bullish", spot=100.0, max_loss_usd=500.0)
        assert note == "ok" and plan is not None
        assert plan.right == "call"
        assert plan.long_strike == 100.0 and plan.short_strike == 105.0   # ATM / ~5% OTM
        assert plan.net_debit == 2.0 and plan.width == 5.0                 # 3.5 - 1.5
        assert plan.contracts == 2 and plan.max_loss_usd == 400.0          # 500 // 200
        assert plan.max_profit_usd == 600.0 and plan.breakeven == 102.0
        assert [l["side"] for l in plan.legs] == ["buy", "sell"]
        assert all(l["qty"] == 2 and l["right"] == "call" for l in plan.legs)

    def test_bearish_debit_put_vertical(self):
        from trading.analytics.options import build_vertical
        plan, note = build_vertical(_rows(), direction="bearish", spot=100.0, max_loss_usd=500.0)
        assert note == "ok" and plan.right == "put"
        assert plan.long_strike == 100.0 and plan.short_strike == 95.0
        assert plan.net_debit == 1.5 and plan.contracts == 3                # 500 // 150
        assert plan.breakeven == 98.5

    def test_budget_too_small_returns_reason(self):
        from trading.analytics.options import build_vertical
        plan, note = build_vertical(_rows(), direction="bullish", spot=100.0, max_loss_usd=50.0)
        assert plan is None and "too small" in note

    def test_bad_direction_and_no_rows(self):
        from trading.analytics.options import build_vertical
        assert build_vertical(_rows(), direction="sideways", spot=100.0, max_loss_usd=500.0)[0] is None
        assert build_vertical([], direction="bullish", spot=100.0, max_loss_usd=500.0)[0] is None


class TestProposeVerticalTool:
    def test_one_call_registers_defined_risk_draft(self, tmp_path):
        config = make_config()
        journal = Journal(tmp_path / "j.db")
        ctx = ToolContext(config=config, journal=journal,
                          broker=OptionBroker(_multi_chain()), account_state=make_account(),
                          agent_name="strategy")
        reg = ToolRegistry(ctx, STRATEGY_TOOLS)
        out = reg.dispatch("propose_vertical", {
            "symbol": "AAPL", "direction": "bullish",
            "thesis": "post-earnings continuation", "expected_edge_usd": 300,
        })
        assert "registered for risk review" in out
        assert len(ctx.drafts) == 1
        d = ctx.drafts[0]
        assert d.asset_class == "option" and len(d.legs) == 2
        assert d.strategy_tag == "debit-call-vertical"
        # Independently defined-risk and within the options cap.
        analysis = analyze_option_legs(d.legs, underlying_shares_held=0, cash_available=1e6)
        assert analysis.is_defined_risk
        assert analysis.max_loss_usd <= config.limits.options.max_loss_per_trade_usd

    def test_budget_too_small_is_actionable_error_not_a_draft(self, tmp_path):
        config = make_config()
        journal = Journal(tmp_path / "j.db")
        ctx = ToolContext(config=config, journal=journal,
                          broker=OptionBroker(_multi_chain()), account_state=make_account(),
                          agent_name="strategy")
        reg = ToolRegistry(ctx, STRATEGY_TOOLS)
        out = reg.dispatch("propose_vertical", {
            "symbol": "AAPL", "direction": "bullish", "thesis": "x",
            "expected_edge_usd": 100, "max_loss_usd": 50,
        })
        assert out.startswith("error:") and not ctx.drafts

    def test_non_strategy_role_never_gets_propose_vertical(self):
        from trading.tools.registry import READ_ONLY_TOOLS
        assert "propose_vertical" not in READ_ONLY_TOOLS
        assert "propose_order" not in READ_ONLY_TOOLS

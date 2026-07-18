import pytest

from trading.broker.models import AccountState, PositionView, Quote
from trading.config import (
    AgentSettings, Config, CostHurdle, Limits, LifecycleGates, LiveGate,
    LossKillSwitch, OptionsLimits, OrderLimits, Paths, PdtLimits, PositionLimits,
    Schedule, Secrets, Settings, SymbolLimits, TaxRates, Universe, WashSaleLimits,
)
from trading.data.journal import Journal
from trading.guardrails.engine import OrderPipeline


def make_config(**limit_overrides) -> Config:
    limits = Limits(
        mode="paper",
        position=PositionLimits(
            max_position_pct=10.0, max_position_usd=5000.0,
            max_open_positions=8, risk_per_trade_pct=1.0,
        ),
        orders=OrderLimits(
            max_order_notional_usd=5000.0, max_new_trades_per_day=5,
            max_new_trades_per_week=15, allow_market_orders=False,
        ),
        loss_kill_switch=LossKillSwitch(max_daily_loss_pct=3.0),
        symbols=SymbolLimits(min_price=5.0, min_avg_daily_volume=500000),
        options=OptionsLimits(
            defined_risk_only=True, max_loss_per_trade_usd=500.0,
            min_days_to_expiry=7, max_contracts_per_order=10,
        ),
        pdt=PdtLimits(enforce=True, equity_threshold_usd=25000.0, max_day_trades_per_5_days=3),
        wash_sale=WashSaleLimits(enforce=True, window_days=30),
        cost_hurdle=CostHurdle(
            enforce=True, min_edge_multiple=2.0,
            option_fee_per_contract_usd=0.05, slippage_bps=5,
        ),
        live=LiveGate(approval_required=True, auto_submit_below_usd=0.0),
        lifecycle=LifecycleGates(),
    )
    limits = limits.model_copy(update=limit_overrides)
    settings = Settings(
        universe=Universe(core=["SPY", "AAPL"]),
        schedule=Schedule(),
        tax=TaxRates(federal_short_term_rate=0.24, federal_long_term_rate=0.15, state_rate=0.05),
        paths=Paths(),
        agents=AgentSettings(),
    )
    return Config(limits=limits, settings=settings, secrets=Secrets())


def make_account(
    equity: float = 50000.0, cash: float | None = None, daily_pl_pct: float = 0.0,
    daytrade_count: int = 0, positions: list[PositionView] | None = None,
) -> AccountState:
    last_equity = equity / (1 + daily_pl_pct / 100.0) if daily_pl_pct else equity
    return AccountState(
        mode="paper",
        equity=equity,
        cash=cash if cash is not None else equity,
        buying_power=equity * 2,
        last_equity=last_equity,
        daytrade_count=daytrade_count,
        pattern_day_trader=False,
        positions=positions or [],
    )


def make_quote(symbol: str = "AAPL", bid: float = 99.9, ask: float = 100.1) -> Quote:
    return Quote(symbol=symbol, bid=bid, ask=ask)


@pytest.fixture()
def config() -> Config:
    return make_config()


@pytest.fixture()
def journal(tmp_path) -> Journal:
    j = Journal(tmp_path / "journal.db")
    yield j
    j.close()


@pytest.fixture()
def pipeline(config, journal) -> OrderPipeline:
    # broker=None: orders are journaled but never sent anywhere in tests
    return OrderPipeline(config, journal, broker=None)

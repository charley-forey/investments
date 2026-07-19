"""Typed configuration: limits.yaml (safety) + settings.yaml (behavior) + .env (secrets)."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Literal

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class PositionLimits(BaseModel):
    max_position_pct: float = Field(gt=0, le=100)
    max_position_usd: float = Field(gt=0)
    max_open_positions: int = Field(gt=0)
    risk_per_trade_pct: float = Field(gt=0, le=100)


class OrderLimits(BaseModel):
    max_order_notional_usd: float = Field(gt=0)
    max_new_trades_per_day: int = Field(ge=0)
    max_new_trades_per_week: int = Field(ge=0)
    allow_market_orders: bool = False
    stale_order_ttl_minutes: int = Field(default=30, ge=0)
    bracket_default_target_r: float = Field(default=2.0, ge=0)


class PortfolioLimits(BaseModel):
    max_gross_exposure_pct: float = Field(default=150.0, gt=0)
    max_positions_per_underlying: int = Field(default=2, gt=0)
    drawdown_circuit_pct: float = Field(default=15.0, ge=0)   # peak-to-trough halt; 0=off
    elevated_vol_gross_scale: float = Field(default=0.5, gt=0)  # gross cap x this in high-vol
    max_position_correlation: float = Field(default=0.9, ge=0)  # >=1 disables the check
    vol_target_annual: float = Field(default=0.0, ge=0)         # 0 = fixed-risk sizing
    kelly_cap: float = Field(default=0.25, ge=0)


class Reconciliation(BaseModel):
    halt_on_mismatch: bool = True
    tolerance_shares: float = Field(default=1.0, ge=0)


class LossKillSwitch(BaseModel):
    max_daily_loss_pct: float = Field(gt=0)
    requires_manual_reset: bool = True


class SymbolLimits(BaseModel):
    min_price: float = Field(ge=0)
    min_avg_daily_volume: int = Field(ge=0)
    blocklist: list[str] = []
    allow_leveraged_etfs: bool = False


class OptionsLimits(BaseModel):
    defined_risk_only: bool = True
    max_loss_per_trade_usd: float = Field(gt=0)
    min_days_to_expiry: int = Field(ge=0)
    max_contracts_per_order: int = Field(gt=0)


class PdtLimits(BaseModel):
    enforce: bool = True
    equity_threshold_usd: float = 25000.0
    max_day_trades_per_5_days: int = 3


class WashSaleLimits(BaseModel):
    enforce: bool = True
    window_days: int = 30
    # "block": guardrail refuses the re-buy (avoids wash sales entirely, default).
    # "defer": allow the re-buy; the tax module defers the disallowed loss into the
    #          replacement lot's basis (the actual IRS treatment).
    mode: Literal["block", "defer"] = "block"


class CostHurdle(BaseModel):
    enforce: bool = True
    min_edge_multiple: float = Field(gt=0)
    option_fee_per_contract_usd: float = Field(ge=0)
    slippage_bps: float = Field(ge=0)


class LiveGate(BaseModel):
    approval_required: bool = True
    auto_submit_below_usd: float = Field(ge=0, default=0.0)


class LifecycleGates(BaseModel):
    paper_to_live_min_trades: int = 30
    paper_to_live_min_expectancy: float = 0.0
    demote_after_losing_weeks: int = 2


class Limits(BaseModel):
    mode: Literal["paper", "live"] = "paper"
    position: PositionLimits
    orders: OrderLimits
    loss_kill_switch: LossKillSwitch
    symbols: SymbolLimits
    options: OptionsLimits
    pdt: PdtLimits
    wash_sale: WashSaleLimits
    cost_hurdle: CostHurdle
    live: LiveGate
    lifecycle: LifecycleGates
    portfolio: PortfolioLimits = PortfolioLimits()
    reconciliation: Reconciliation = Reconciliation()


class Schedule(BaseModel):
    timezone: str = "America/New_York"
    premarket_research: str = "08:30"
    intraday_scan_every_minutes: int = 15
    postclose_review: str = "16:30"
    weekend_research_day: str = "sat"
    weekend_research_time: str = "10:00"
    intel_every_minutes: int = 10          # continuous market-intelligence ingestion


class TaxRates(BaseModel):
    federal_short_term_rate: float = Field(ge=0, le=1)
    federal_long_term_rate: float = Field(ge=0, le=1)
    state_rate: float = Field(ge=0, le=1)

    @property
    def short_term_total(self) -> float:
        return self.federal_short_term_rate + self.state_rate

    @property
    def long_term_total(self) -> float:
        return self.federal_long_term_rate + self.state_rate


class Paths(BaseModel):
    journal_db: str = "data/journal.db"
    bars_dir: str = "data/bars"
    bars_db: str = "data/bars.db"
    intel_db: str = "data/intel.db"
    vectors_db: str = "data/vectors.db"
    memory_dir: str = "memory"
    playbooks_dir: str = "playbooks"
    calendar_file: str = "data/calendar.json"  # user-provided events feed (optional)

    def resolve(self, root: Path) -> "Paths":
        return Paths(
            journal_db=str(root / self.journal_db),
            bars_dir=str(root / self.bars_dir),
            bars_db=str(root / self.bars_db),
            intel_db=str(root / self.intel_db),
            vectors_db=str(root / self.vectors_db),
            memory_dir=str(root / self.memory_dir),
            playbooks_dir=str(root / self.playbooks_dir),
            calendar_file=str(root / self.calendar_file),
        )


class AgentSettings(BaseModel):
    model: str = "claude-opus-4-8"
    max_tokens: int = 16000
    max_proposals_per_cycle: int = 2
    max_tool_iterations: int = 25
    bars_lookback_days: int = 30
    news_limit: int = 10
    options_chain_strikes: int = 5
    options_chain_max_dte: int = 60
    # Cost-aware routing: per-role model overrides (None -> use `model`). Route a
    # cheap model to screening/watchlists and the top model to real decisions.
    strategy_model: str | None = None
    risk_model: str | None = None
    scoring_model: str | None = None
    redteam_model: str | None = None
    # Adversarial red-team pass triggers at/above this proposal confidence.
    # 1.0 effectively disables it (default); lower it to enable.
    redteam_confidence_threshold: float = 1.0

    def model_for(self, role: str) -> str:
        return getattr(self, f"{role}_model", None) or self.model


class Universe(BaseModel):
    core: list[str] = []


class Settings(BaseModel):
    universe: Universe
    schedule: Schedule
    tax: TaxRates
    paths: Paths
    agents: AgentSettings


class Secrets(BaseModel):
    """Broker/API credentials from .env. Selected by mode so live keys are never
    touched while mode=paper."""

    alpaca_api_key: str = ""
    alpaca_secret_key: str = ""
    anthropic_api_key: str = ""
    discord_webhook_url: str = ""

    @property
    def has_alpaca(self) -> bool:
        return bool(self.alpaca_api_key and self.alpaca_secret_key)


class Config(BaseModel):
    limits: Limits
    settings: Settings
    secrets: Secrets

    @property
    def is_live(self) -> bool:
        return self.limits.mode == "live"


def _load_yaml(path: Path) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_config(root: Path | None = None) -> Config:
    root = root or PROJECT_ROOT
    load_dotenv(root / ".env")

    limits = Limits.model_validate(_load_yaml(root / "config" / "limits.yaml"))
    settings = Settings.model_validate(_load_yaml(root / "config" / "settings.yaml"))
    settings = settings.model_copy(update={"paths": settings.paths.resolve(root)})

    prefix = "ALPACA_LIVE" if limits.mode == "live" else "ALPACA_PAPER"
    secrets = Secrets(
        alpaca_api_key=os.getenv(f"{prefix}_API_KEY", ""),
        alpaca_secret_key=os.getenv(f"{prefix}_SECRET_KEY", ""),
        anthropic_api_key=os.getenv("ANTHROPIC_API_KEY", ""),
        discord_webhook_url=os.getenv("DISCORD_WEBHOOK_URL", ""),
    )
    return Config(limits=limits, settings=settings, secrets=secrets)


@lru_cache(maxsize=1)
def get_config() -> Config:
    return load_config()

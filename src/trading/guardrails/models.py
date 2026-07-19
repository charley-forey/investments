"""Order proposal models — the shape every trade idea must take before it can
reach the guardrail engine, whether it came from an agent or a human."""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, Field


class OptionLeg(BaseModel):
    side: Literal["buy", "sell"]
    right: Literal["call", "put"]
    strike: float = Field(gt=0)
    expiry: date
    qty: int = Field(gt=0)                 # contracts
    est_premium: float = Field(ge=0)       # per share (multiply by 100 for per contract)
    occ_symbol: str | None = None          # OCC option symbol once known


class OrderProposal(BaseModel):
    agent: str = "human"
    strategy_tag: str = "manual"
    symbol: str                            # underlying (options) or ticker (stocks)
    asset_class: Literal["stock", "option"] = "stock"
    side: Literal["buy", "sell"] = "buy"   # net direction for stocks; ignored for multi-leg
    qty: float = Field(default=0, ge=0)    # shares for stocks; 0 for options (qty on legs)
    order_type: Literal["limit", "market"] = "limit"
    limit_price: float | None = Field(default=None, gt=0)
    stop_price: float | None = Field(default=None, gt=0)   # protective stop (bracket + sizing)
    target_price: float | None = Field(default=None, gt=0)  # take-profit (bracket)
    legs: list[OptionLeg] = []
    thesis: str | None = None
    expected_edge_usd: float | None = None  # agent's stated expected profit
    max_loss_usd: float | None = None       # agent's stated max loss (recomputed independently)
    confidence: float | None = Field(default=None, ge=0, le=1)
    reduces_position: bool = False          # closing/trimming an existing position

    def model_post_init(self, __context) -> None:
        self.symbol = self.symbol.upper()

    @property
    def is_option(self) -> bool:
        return self.asset_class == "option"


class Violation(BaseModel):
    rule: str
    message: str


class GuardrailResult(BaseModel):
    approved: bool
    violations: list[Violation] = []
    est_cost_usd: float | None = None
    computed_max_loss_usd: float | None = None
    notional_usd: float | None = None

    @property
    def reasons(self) -> str:
        return "; ".join(f"[{v.rule}] {v.message}" for v in self.violations)


class PipelineResult(BaseModel):
    proposal_id: int
    status: str                # rejected | pending_approval | submitted
    result: GuardrailResult
    order_id: int | None = None
    broker_order_id: str | None = None

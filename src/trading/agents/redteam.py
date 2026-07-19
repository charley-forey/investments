"""Red-team agent: an adversarial second review of high-conviction proposals.

Reuses the risk agent's structured-verdict machinery but with an adversarial
system prompt and its own (optionally stronger) model. Runs only for proposals at
or above the configured confidence threshold — the trades where an extra
challenge is worth the cost.
"""

from __future__ import annotations

from ..broker.models import AccountState
from ..config import Config
from ..data.journal import Journal
from ..guardrails.models import OrderProposal
from . import prompts
from .risk import RiskVerdict, _limits_summary, _proposal_summary
from .risk import review_proposal as _review_with


def should_red_team(config: Config, proposal: OrderProposal) -> bool:
    threshold = config.settings.agents.redteam_confidence_threshold
    if threshold >= 1.0:
        return False
    return (proposal.confidence or 0.0) >= threshold


def red_team(
    client, config: Config, journal: Journal, broker, account: AccountState,
    proposal: OrderProposal,
) -> RiskVerdict:
    """Adversarial review. Implemented via the shared risk-review loop with the
    red-team system prompt and model, so verdicts are structured and journaled the
    same way."""
    return _review_with(
        client, config, journal, broker, account, proposal,
        system_prompt=prompts.RED_TEAM_SYSTEM,
        model=config.settings.agents.model_for("redteam"),
        agent_name="redteam",
    )

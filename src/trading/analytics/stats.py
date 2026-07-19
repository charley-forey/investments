"""Deterministic per-strategy performance statistics from the scores table.

Everything here is pure arithmetic over recorded closed trades — the numbers that
decide capital allocation, so they must be reproducible and testable. Reuses the
after-tax math in guardrails.account_math."""

from __future__ import annotations

from dataclasses import dataclass

from ..config import TaxRates
from ..data.journal import Journal
from ..guardrails.account_math import after_tax_pnl


@dataclass
class StrategyStats:
    strategy_tag: str
    trades: int
    wins: int
    losses: int
    gross_pnl: float
    after_tax_pnl: float
    win_rate: float
    avg_win: float
    avg_loss: float
    expectancy: float             # gross average P&L per trade
    after_tax_expectancy: float   # net-of-tax average P&L per trade
    max_drawdown: float           # peak-to-trough on the cumulative gross curve

    def summary(self) -> str:
        return (
            f"{self.strategy_tag}: {self.trades} trades, win {self.win_rate*100:.0f}%, "
            f"expectancy ${self.expectancy:+.2f} (after-tax ${self.after_tax_expectancy:+.2f}), "
            f"gross ${self.gross_pnl:+.2f}, maxDD ${self.max_drawdown:.2f}"
        )


def _term_of(score: dict) -> str:
    notes = score.get("notes") or ""
    return "long" if "term=long" in notes else "short"


def compute_stats(scores: list[dict], rates: TaxRates) -> StrategyStats | None:
    if not scores:
        return None
    tag = scores[0].get("strategy_tag") or "untagged"
    pnls = [float(s["pnl_usd"] or 0.0) for s in scores]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    n = len(pnls)
    gross = sum(pnls)
    after_tax = sum(after_tax_pnl(float(s["pnl_usd"] or 0.0), _term_of(s), rates) for s in scores)

    # Max drawdown on the cumulative gross curve.
    cum = 0.0
    peak = 0.0
    max_dd = 0.0
    for p in pnls:
        cum += p
        peak = max(peak, cum)
        max_dd = max(max_dd, peak - cum)

    return StrategyStats(
        strategy_tag=tag,
        trades=n,
        wins=len(wins),
        losses=len(losses),
        gross_pnl=gross,
        after_tax_pnl=after_tax,
        win_rate=len(wins) / n if n else 0.0,
        avg_win=sum(wins) / len(wins) if wins else 0.0,
        avg_loss=sum(losses) / len(losses) if losses else 0.0,
        expectancy=gross / n if n else 0.0,
        after_tax_expectancy=after_tax / n if n else 0.0,
        max_drawdown=max_dd,
    )


def stats_by_tag(journal: Journal, rates: TaxRates) -> dict[str, StrategyStats]:
    out: dict[str, StrategyStats] = {}
    for tag in journal.distinct_strategy_tags():
        s = compute_stats(journal.scores_for_tag(tag), rates)
        if s is not None:
            out[tag] = s
    return out


def portfolio_summary(journal: Journal, rates: TaxRates) -> str:
    by_tag = stats_by_tag(journal, rates)
    if not by_tag:
        return "no closed trades scored yet"
    lines = ["Per-strategy performance:"]
    for tag in sorted(by_tag):
        lines.append("  " + by_tag[tag].summary())
    total = sum(s.gross_pnl for s in by_tag.values())
    total_at = sum(s.after_tax_pnl for s in by_tag.values())
    lines.append(f"Total realized: gross ${total:+.2f}, after-tax ${total_at:+.2f}")
    return "\n".join(lines)

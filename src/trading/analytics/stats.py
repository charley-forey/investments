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


def _round_trips(scores: list[dict]) -> list[float]:
    """Collapse per-lot score rows into one P&L per round trip.

    A score row is a closed TAX LOT, not a trade. One SMCI position entered on six
    partial fills and exited on two produced seven rows, and the EOD review duly
    reported "7 trades, expectancy $-2.40" for what was a single -$16.83 position.
    That count is not cosmetic: it is `stats.trades`, which gates
    paper_to_live_min_trades (30) and sizes the small-sample shrink in
    autocalibrate. Left alone, one position exited in 30 partial fills clears the
    bar for trading real money.

    Lots are grouped by the proposal that opened them. Rows with no proposal_id
    stay individual — unattributable, so the safe reading is the conservative one.
    """
    groups: dict = {}
    order: list = []
    for i, s in enumerate(scores):
        pid = s.get("proposal_id")
        key = ("prop", int(pid)) if pid is not None else ("row", s.get("id", i))
        if key not in groups:
            groups[key] = 0.0
            order.append(key)
        groups[key] += float(s["pnl_usd"] or 0.0)
    return [groups[k] for k in order]


def compute_stats(scores: list[dict], rates: TaxRates) -> StrategyStats | None:
    if not scores:
        return None
    tag = scores[0].get("strategy_tag") or "untagged"
    pnls = _round_trips(scores)
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    n = len(pnls)
    gross = sum(pnls)
    # After-tax stays per-lot: the holding term is a property of the lot, not of the
    # round trip, and a position can straddle the long/short boundary.
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
    # Labelled all-time because it is: scores_for_tag has no date filter. The note
    # is written daily, so an unlabelled block reads as "today" — the 7/28 EOD
    # review restated a 5-day-old total under a 7/28 heading.
    lines = ["Per-strategy performance (all-time, closed round trips):"]
    for tag in sorted(by_tag):
        lines.append("  " + by_tag[tag].summary())
    total = sum(s.gross_pnl for s in by_tag.values())
    total_at = sum(s.after_tax_pnl for s in by_tag.values())
    lines.append(f"Total realized: gross ${total:+.2f}, after-tax ${total_at:+.2f}")
    return "\n".join(lines)


def open_positions_summary(account) -> str:
    """Open risk at the close. Absent from the EOD note entirely, so a day whose
    only activity was opening a position read as a day with no activity at all."""
    positions = getattr(account, "positions", None) or []
    if not positions:
        return "Open positions: none (flat)."
    lines = ["Open positions (unrealized, not in the totals above):"]
    total = 0.0
    for p in positions:
        upl = float(getattr(p, "unrealized_pl", 0.0) or 0.0)
        total += upl
        lines.append(f"  {p.symbol}: {p.qty:g} @ {getattr(p, 'avg_entry_price', 0):.2f}"
                     f" -> unrealized ${upl:+.2f}")
    lines.append(f"  Total unrealized: ${total:+.2f}")
    return "\n".join(lines)

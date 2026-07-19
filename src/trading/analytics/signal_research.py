"""Social/news -> outcome learning: join stored sentiment snapshots to the price
move that followed, so the system discovers which social conditions actually preceded
moves. Discovered conditions become tagged candidate strategies that the M8
walk-forward gate then vets — research-driven learning on top of the trade-outcome
loop. Pure computation; no external data."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class SignalStudy:
    symbol: str
    samples: int
    avg_forward_return: float     # mean next-horizon return after a positive-polarity read
    hit_rate: float               # fraction of positive-polarity reads followed by a gain

    def summary(self) -> str:
        return (f"{self.symbol}: {self.samples} samples, "
                f"avg fwd return {self.avg_forward_return*100:+.2f}%, "
                f"hit rate {self.hit_rate*100:.0f}%")


def _close_on_or_after(bars: list, date_str: str):
    for b in bars:
        if b.date >= date_str:
            return b.close
    return None


def study_sentiment_signal(
    sentiment_history: list[dict], bars: list, *,
    polarity_threshold: float = 0.2, horizon_days: int = 5,
) -> SignalStudy | None:
    """For each positive-polarity sentiment reading, measure the forward return over
    `horizon_days`. `bars` is a list with .date (YYYY-MM-DD) and .close."""
    if not sentiment_history or not bars:
        return None
    dates = [b.date for b in bars]
    fwd_returns = []
    for snap in sentiment_history:
        if (snap.get("polarity") or 0) < polarity_threshold:
            continue
        d = (snap.get("ts") or "")[:10]
        entry = _close_on_or_after(bars, d)
        if entry is None or entry <= 0:
            continue
        # forward bar ~horizon trading days later
        try:
            i = next(idx for idx, dd in enumerate(dates) if dd >= d)
        except StopIteration:
            continue
        j = min(i + horizon_days, len(bars) - 1)
        if j <= i:
            continue
        fwd_returns.append((bars[j].close - entry) / entry)

    if not fwd_returns:
        return None
    avg = sum(fwd_returns) / len(fwd_returns)
    hits = sum(1 for r in fwd_returns if r > 0) / len(fwd_returns)
    sym = sentiment_history[0]["symbol"]
    return SignalStudy(symbol=sym, samples=len(fwd_returns),
                       avg_forward_return=round(avg, 4), hit_rate=round(hits, 3))


def is_promising(study: SignalStudy | None, *, min_samples: int = 20,
                 min_avg_return: float = 0.0, min_hit_rate: float = 0.5) -> bool:
    """Whether a discovered sentiment->price relationship is worth proposing as a
    candidate strategy for walk-forward vetting."""
    return bool(study and study.samples >= min_samples
                and study.avg_forward_return > min_avg_return
                and study.hit_rate >= min_hit_rate)

"""Helpers to populate semantic memory so recall_similar returns real history.

Writes are best-effort: a vector failure must never break a trading cycle.
"""

from __future__ import annotations


def _store(config):
    from .vectors import VectorStore

    path = config.settings.paths.vectors_db
    return VectorStore(path)


def remember_proposal(config, proposal_id: int, *, symbol: str, strategy_tag: str,
                      thesis: str | None, status: str,
                      outcome_note: str | None = None) -> None:
    """Index a proposal thesis (and optional outcome) for semantic recall."""
    try:
        text = (f"{status} {strategy_tag} {symbol}: {thesis or ''}".strip())
        if outcome_note:
            text = f"{text} | {outcome_note}"
        store = _store(config)
        try:
            store.add("proposal", str(proposal_id), text[:2000])
        finally:
            store.close()
    except Exception:
        pass


def remember_lessons(config, lessons: list[str]) -> None:
    """Index each new lesson line."""
    if not lessons:
        return
    try:
        import hashlib
        store = _store(config)
        try:
            for lesson in lessons:
                ref = hashlib.md5(lesson.encode("utf-8")).hexdigest()[:12]
                store.add("lesson", ref, lesson[:2000])
        finally:
            store.close()
    except Exception:
        pass


def remember_digest(config, digest: str, *, scope: str = "market") -> None:
    """Index a market-intel digest."""
    if not digest:
        return
    try:
        from datetime import datetime, timezone
        ref = f"{scope}:{datetime.now(timezone.utc).strftime('%Y%m%d%H%M')}"
        store = _store(config)
        try:
            store.add("digest", ref, digest[:2000])
        finally:
            store.close()
    except Exception:
        pass


def remember_outcome(config, proposal_id: int, *, symbol: str, hyp_pnl: float,
                     verdict_was_right: bool | None, notes: str | None) -> None:
    """Update the proposal vector with the counterfactual/realized outcome."""
    right = ("RIGHT pass" if verdict_was_right else
             ("WRONG pass" if verdict_was_right is False else "ungraded"))
    remember_proposal(
        config, proposal_id, symbol=symbol, strategy_tag="outcome",
        thesis=f"hyp_pnl ${hyp_pnl:+.0f} ({right})", status="outcome",
        outcome_note=notes,
    )

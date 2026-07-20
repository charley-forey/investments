"""Per-interval signal snapshot of the whole universe — a regular time-series
research dataset. Deterministic (no LLM): each intraday cycle records one row per
symbol with the quote, technical features, and latest sentiment the agent had
available. Best-effort per symbol: a data failure yields a row with NULL fields
rather than aborting, so the grid stays complete."""

from __future__ import annotations

import os

from .features import compute_features


def _feature_rows(df):
    """Adapt a bars DataFrame into the .open/.high/.low/.close shape compute_features
    expects (same shim the get_features tool uses)."""
    return [type("B", (), {"open": float(r["open"]), "high": float(r["high"]),
                           "low": float(r["low"]), "close": float(r["close"])})()
            for _, r in df.iterrows()]


def _snapshot_symbol(config, journal, broker, store, symbol: str) -> dict:
    bid = ask = last = spread_bps = sentiment = mention_count = None
    features = None
    realized_vol = None
    try:
        q = broker.get_quote(symbol)
        bid, ask = q.bid, q.ask
        last = q.mid
        spread_bps = (q.spread / q.mid * 10000) if q.mid else None
    except Exception:
        pass
    try:
        days = max(config.settings.agents.bars_lookback_days, 40)
        df = broker.get_bars(symbol, days=days)
        if df is not None and len(df):
            feats = compute_features(_feature_rows(df))
            if feats:
                features = feats.as_dict()
                realized_vol = feats.realized_vol
                if last is None:
                    last = feats.last
    except Exception:
        pass
    if store is not None:
        try:
            hist = store.sentiment_history(symbol, days=5)
            if hist:
                sentiment = hist[-1]["polarity"]
                mention_count = hist[-1]["mention_count"]
        except Exception:
            pass
    atm_iv, iv_rank_val, pc_skew = _option_signals(journal, broker, symbol, last, realized_vol)
    return {"bid": bid, "ask": ask, "last": last, "spread_bps": spread_bps,
            "features": features, "sentiment": sentiment,
            "mention_count": mention_count,
            "atm_iv": atm_iv, "iv_rank": iv_rank_val, "pc_skew": pc_skew}


def _option_signals(journal, broker, symbol: str, spot, realized_vol):
    """Best-effort ATM IV, IV rank, and put/call skew from the option chain."""
    from .options import chain_rows, chain_signals, iv_rank

    try:
        chain = broker.get_options_chain(symbol)
    except Exception:
        return None, None, None
    if not chain or not spot:
        return None, None, None
    try:
        rows = chain_rows(chain, symbol, spot)
        sig = chain_signals(rows, realized_vol)
        hist = [r["atm_iv"] for r in journal.conn.execute(
            "SELECT atm_iv FROM signal_snapshot WHERE symbol=? AND atm_iv IS NOT NULL "
            "ORDER BY id DESC LIMIT 200", (symbol.upper(),)).fetchall()]
        return sig.atm_iv, iv_rank(sig.atm_iv, hist), sig.pc_skew
    except Exception:
        return None, None, None


def snapshot_universe(config, journal, broker, *, cycle: str = "intraday") -> int:
    """Record one signal_snapshot row per universe symbol. Returns rows written."""
    from ..data.intel import IntelStore

    store = None
    intel_path = config.settings.paths.intel_db
    if os.path.exists(intel_path):
        store = IntelStore(intel_path)
    try:
        n = 0
        for symbol in config.settings.universe.core:
            s = _snapshot_symbol(config, journal, broker, store, symbol)
            journal.record_snapshot(cycle=cycle, symbol=symbol, **s)
            n += 1
        return n
    finally:
        if store is not None:
            store.close()

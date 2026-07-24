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
    # Flag unusable quotes in features so research can filter them later.
    if features is not None and spread_bps is not None and spread_bps > 100:
        features = dict(features)
        features["wide_spread_warning"] = True
        features["spread_pct"] = round(spread_bps / 100.0, 2)
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
    """Record one signal_snapshot row per universe + active candidate symbol."""
    from ..data.intel import IntelStore

    store = None
    intel_path = config.settings.paths.intel_db
    if os.path.exists(intel_path):
        store = IntelStore(intel_path)
    # Regime tag for the whole cycle (one read, applied to every row) so outcomes
    # can be sliced by the tape they occurred in.
    regime_trend = regime_vol = None
    try:
        from ..tools.market_context import market_regime
        reg = market_regime(broker)
        regime_trend, regime_vol = reg.trend, reg.vol_state
    except Exception:
        pass

    # Candidate metadata keyed by symbol, so scanner-candidate rows carry the
    # template/direction that the candidate_grading job scores by forward return.
    cand_meta = {}
    try:
        from ..scanner.movers import load_candidates
        for c in load_candidates(config):
            cand_meta[c["symbol"].upper()] = c
    except Exception:
        pass

    try:
        symbols = list(config.settings.universe.core)
        for s in cand_meta:
            if s not in symbols:
                symbols.append(s)
        n = 0
        for symbol in symbols:
            s = _snapshot_symbol(config, journal, broker, store, symbol)
            c = cand_meta.get(symbol.upper())
            journal.record_snapshot(
                cycle=cycle, symbol=symbol, **s,
                regime_trend=regime_trend, regime_vol=regime_vol,
                template=(c or {}).get("template"),
                trigger_direction=(c or {}).get("trigger_direction"),
                trigger_level=(c or {}).get("trigger_level"),
                cand_score=(c or {}).get("score"),
            )
            n += 1
        return n
    finally:
        if store is not None:
            store.close()

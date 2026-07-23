"""Operations dashboard (FastAPI, localhost-only) — the full transparency + control
cockpit. Every read endpoint reuses existing analytics/journal/broker; the dashboard
adds views and human-triggered actions, never new authority. Order execution still
flows only through the guardrail pipeline.

Trust boundary: the action and config-save endpoints approve proposals, submit
orders and rewrite the risk limits, so the API fails closed. Loopback with no
token stays open (the single-user local workflow). Anything else needs
DASHBOARD_TOKEN set in the environment and presented per request; a non-loopback
request without one is refused. Multi-user accounts and roles are still an M13
blocked item — this is one shared secret, not identity.
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ..config import Limits, Settings, get_config
from ..data.journal import Journal

_STATIC = Path(__file__).parent / "static"


def _ts_gap(a: str | None, b: str | None) -> float:
    """Absolute seconds between two ISO timestamps; large if unparseable."""
    try:
        return abs((datetime.fromisoformat(a) - datetime.fromisoformat(b)).total_seconds())
    except (ValueError, TypeError):
        return 1e18


def _fetch_bars(brk, symbol: str, days: int, timeframe: str) -> list[dict]:
    """Broker OHLCV -> plain dicts. Returns [] rather than raising: a price chart
    is never worth failing a dashboard request over."""
    if brk is None:
        return []
    try:
        df = brk.get_bars(symbol, days=days, timeframe=timeframe)
        if df is None or getattr(df, "empty", True):
            return []
        df = df.reset_index()
        ts_col = "timestamp" if "timestamp" in df.columns else df.columns[1]
        out = []
        for _, r in df.iterrows():
            stamp = r[ts_col]
            date = (stamp.isoformat() if timeframe != "1Day"
                    else str(stamp)[:10])
            out.append({"symbol": symbol.upper(), "date": date,
                        "open": float(r["open"]), "high": float(r["high"]),
                        "low": float(r["low"]), "close": float(r["close"]),
                        "volume": float(r.get("volume", 0) or 0)})
        return out
    except Exception:
        return []


def _yaml_help(path: Path) -> dict[str, str]:
    """Harvest the inline `# ...` comments from a config file as field help text.
    The YAML is already the documentation — this just surfaces it in the UI."""
    help_map: dict[str, str] = {}
    if not path.exists():
        return help_map
    for line in path.read_text(encoding="utf-8").splitlines():
        if "#" not in line or ":" not in line.split("#")[0]:
            continue
        key, comment = line.split("#", 1)[0], line.split("#", 1)[1]
        key = key.split(":")[0].strip().lstrip("- ")
        if key and comment.strip():
            help_map[key] = comment.strip()
    return help_map


def _default_broker_factory(config):
    try:
        from ..broker.alpaca import AlpacaBroker
        return AlpacaBroker(config)
    except Exception:
        return None


_LOOPBACK = {"127.0.0.1", "::1", "localhost", "testclient"}


def create_app(get_config_fn=get_config, broker_factory=None, token=None):
    import os
    import secrets

    from fastapi import FastAPI, HTTPException, Request
    from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

    app = FastAPI(title="Agentic Trading — Operations")
    _make_broker = broker_factory or _default_broker_factory
    _token = token if token is not None else (os.environ.get("DASHBOARD_TOKEN") or None)

    def _supplied(request) -> str:
        return (request.headers.get("x-dashboard-token")
                or (request.headers.get("authorization") or "").removeprefix("Bearer ").strip()
                or request.cookies.get("dash_token") or "")

    @app.middleware("http")
    async def guard(request: Request, call_next):
        """Auth for the API surface.

        These endpoints approve proposals, submit orders and rewrite the risk
        limits, so the rule is fail-closed: a request from anywhere but loopback
        is refused unless it carries the token. Loopback with no token configured
        stays open, which is the existing single-user local workflow.

        The shell and its static assets are served unauthenticated — they hold no
        data, and the page needs to load in order to ask for the token.
        """
        if not request.url.path.startswith("/api"):
            return await call_next(request)
        host = (request.client.host if request.client else "") or ""
        authed = (secrets.compare_digest(_supplied(request), _token) if _token else True)
        # Answered here rather than as a route so the console can ask whether it
        # needs a token without first having one.
        if request.url.path == "/api/auth":
            return JSONResponse({"required": bool(_token), "authenticated": authed,
                                 "loopback": host in _LOOPBACK})
        if _token:
            if not authed:
                return JSONResponse({"detail": "invalid or missing dashboard token"},
                                    status_code=401)
        elif host not in _LOOPBACK:
            return JSONResponse(
                {"detail": "remote access requires a token — start the dashboard with "
                           "DASHBOARD_TOKEN set in the environment"}, status_code=403)
        return await call_next(request)

    def cfg():
        return get_config_fn()

    def journal() -> Journal:
        return Journal(cfg().settings.paths.journal_db)

    def broker():
        return _make_broker(cfg())

    def _account_state(j):
        b = broker()
        if b is None:
            return None
        try:
            return b.get_account_state(j)
        except Exception:
            return None

    # -- overview / metrics ---------------------------------------------------

    @app.get("/api/metrics")
    def metrics():
        from ..monitoring import metrics_snapshot
        return metrics_snapshot(cfg(), journal())

    @app.get("/api/account")
    def account():
        j = journal()
        state = _account_state(j)
        if state is None:
            return {"available": False}
        # Throttled equity snapshot so viewing the dashboard builds the curve.
        last = j.last_equity_ts()
        if last is None or datetime.fromisoformat(last) < datetime.now(timezone.utc) - timedelta(minutes=5):
            try:
                j.record_equity(equity=state.equity, cash=state.cash,
                                buying_power=state.buying_power)
            except Exception:
                pass
        return {"available": True, "mode": state.mode, "equity": state.equity,
                "cash": state.cash, "buying_power": state.buying_power,
                "last_equity": state.last_equity, "daily_pl": state.daily_pl,
                "daily_pl_pct": state.daily_pl_pct,
                "open_positions": state.open_position_count,
                "daytrade_count": state.daytrade_count,
                "pattern_day_trader": state.pattern_day_trader}

    @app.get("/api/positions")
    def positions():
        j = journal()
        state = _account_state(j)
        if state is None:
            return {"available": False, "positions": [], "lots": []}
        return {"available": True,
                "positions": [p.model_dump() for p in state.positions],
                "lots": [l.model_dump() for l in state.lots]}

    @app.get("/api/portfolio_risk")
    def portfolio_risk_endpoint():
        from dataclasses import asdict
        from ..analytics.portfolio_risk import portfolio_risk
        j = journal()
        state = _account_state(j)
        if state is None:
            return {"available": False}
        # Adapt PositionView -> the position shape portfolio_risk expects.
        positions = []
        for p in state.positions:
            price = (p.market_value / p.qty) if p.qty else p.avg_entry_price
            positions.append({"symbol": p.symbol, "qty": p.qty, "price": abs(price),
                              "asset_class": p.asset_class})
        pr = portfolio_risk(positions, state.equity)
        return {"available": True, "summary": pr.summary(), **asdict(pr)}

    @app.get("/api/equity")
    def equity():
        return journal().equity_history(limit=500)

    @app.get("/api/pnl")
    def pnl():
        config = cfg()
        j = journal()
        from ..analytics.stats import stats_by_tag
        rows = j.all_scores()
        realized = round(sum(float(s["pnl_usd"] or 0) for s in rows), 2)
        state = _account_state(j)
        unrealized = round(sum(p.unrealized_pl for p in state.positions), 2) if state else 0.0
        by_tag = {t: asdict(s) for t, s in stats_by_tag(j, config.settings.tax).items()}
        return {"realized": realized, "unrealized": unrealized,
                "total": round(realized + unrealized, 2),
                "closed_trades": len(rows), "per_strategy": by_tag}

    @app.get("/api/trades")
    def trades(limit: int = 100):
        """Orders with their fills — two queries regardless of order count.
        (Was one query per order, which is fine at 9 orders and a stall at 10,000.)"""
        j = journal()
        orders = [dict(o) for o in j.conn.execute(
            "SELECT * FROM orders ORDER BY id DESC LIMIT ?", (limit,)).fetchall()]
        by_order: dict[int, list[dict]] = {}
        if orders:
            ids = [o["id"] for o in orders]
            rows = j.conn.execute(
                f"SELECT * FROM fills WHERE order_id IN ({','.join('?' * len(ids))}) "
                "ORDER BY id", ids).fetchall()
            for f in rows:
                by_order.setdefault(f["order_id"], []).append(dict(f))
        for o in orders:
            o["fills"] = by_order.get(o["id"], [])
        return orders

    @app.get("/api/opportunities")
    def opportunities(limit: int = 50):
        from ..analytics.decision_record import list_records
        out = []
        for r in list_records(journal(), limit):
            reason = next((v.get("reason") for v in reversed(r.verdicts)
                           if v.get("reason")), None)
            out.append({"proposal_id": r.proposal_id, "ts": r.ts, "symbol": r.symbol,
                        "strategy_tag": r.strategy_tag, "side": r.side, "qty": r.qty,
                        "status": r.status, "thesis": r.thesis,
                        "expected_edge_usd": r.expected_edge_usd,
                        "confidence": r.confidence, "reason": reason,
                        "verdicts": r.verdicts})
        return out

    @app.get("/api/performance")
    def performance():
        config = cfg()
        j = journal()
        from ..analytics.allocation import allocate_capital, attribution_report
        from ..analytics.lifecycle import get_stage
        from ..analytics.stats import stats_by_tag
        stats = stats_by_tag(j, config.settings.tax)
        return {
            "per_strategy": {t: {**asdict(s), "stage": get_stage(j, t)}
                             for t, s in stats.items()},
            "allocation": [asdict(a) for a in allocate_capital(j, config.settings.tax)],
            "attribution": [asdict(a) for a in attribution_report(j, config.settings.tax)],
        }

    @app.get("/api/edge")
    def edge():
        from ..analytics.edge import benchmark_comparison, portfolio_edge, strategy_edges
        from dataclasses import asdict as _asdict
        c = cfg()
        j = journal()
        return {"strategies": [_asdict(e) for e in strategy_edges(j)],
                "portfolio": portfolio_edge(j),
                "benchmark": benchmark_comparison(j, c.settings.paths.bars_db)}

    @app.get("/api/outcomes")
    def outcomes():
        """Was the agent right? Counterfactual grades for proposals it declined,
        realized scores for trades it took, and confidence calibration.

        When these are empty it is almost always because nothing is *eligible* yet,
        not because grading is broken — so the payload also reports what the
        pipeline is waiting on, and when each pending item becomes gradable.
        """
        from ..analytics.calibration import build_calibration_report
        from ..analytics.counterfactuals import DEFAULT_HORIZON_DAYS, DEFAULT_MIN_AGE_DAYS
        j = journal()
        rows = [dict(r) for r in j.conn.execute(
            "SELECT o.*, p.symbol, p.side, p.status, p.strategy_tag, p.confidence, "
            "p.thesis, p.ts AS proposal_ts FROM proposal_outcomes o "
            "JOIN proposals p ON p.id = o.proposal_id ORDER BY o.id DESC").fetchall()]
        cal = build_calibration_report(j)

        # What is still in the queue, and when it becomes gradable.
        cutoff = datetime.now(timezone.utc) - timedelta(days=DEFAULT_MIN_AGE_DAYS)
        waiting = []
        for p in j.conn.execute(
            "SELECT p.id, p.ts, p.symbol, p.status, p.asset_class FROM proposals p "
            "LEFT JOIN proposal_outcomes o ON o.proposal_id = p.id "
            "WHERE p.status IN ('vetoed','rejected') AND o.id IS NULL ORDER BY p.id"
        ).fetchall():
            try:
                gradable = datetime.fromisoformat(p["ts"]) + timedelta(days=DEFAULT_MIN_AGE_DAYS)
            except (ValueError, TypeError):
                continue
            waiting.append({
                "id": p["id"], "symbol": p["symbol"], "status": p["status"],
                "ts": p["ts"], "gradable_on": gradable.isoformat(),
                "blocked_by": ("not yet aged" if datetime.fromisoformat(p["ts"]) > cutoff
                               else "options proposal — not graded"
                               if (p["asset_class"] or "stock") != "stock"
                               else "awaiting the next end-of-day cycle"),
            })
        lots = j.conn.execute(
            "SELECT COUNT(*) AS n, SUM(close_ts IS NOT NULL) AS closed, "
            "SUM(close_ts IS NOT NULL AND scored=0) AS unscored FROM tax_lots").fetchone()
        return {
            "outcomes": rows,
            "calibration": {**asdict(cal)},
            "pipeline": {
                "min_age_days": DEFAULT_MIN_AGE_DAYS,
                "horizon_days": DEFAULT_HORIZON_DAYS,
                "waiting": waiting,
                "lots_total": lots["n"] or 0,
                "lots_closed": lots["closed"] or 0,
                "lots_closed_unscored": lots["unscored"] or 0,
                "broker_available": broker() is not None,
            },
        }

    @app.get("/api/decisions")
    def decisions(limit: int = 25):
        from ..analytics.decision_record import list_records
        return [{"proposal_id": r.proposal_id, "ts": r.ts, "strategy_tag": r.strategy_tag,
                 "symbol": r.symbol, "side": r.side, "qty": r.qty, "status": r.status,
                 "thesis": r.thesis} for r in list_records(journal(), limit)]

    @app.get("/api/decisions/{proposal_id}")
    def decision(proposal_id: int):
        from ..analytics.decision_record import build_record
        rec = build_record(journal(), proposal_id)
        if rec is None:
            raise HTTPException(404, "no such decision")
        d = asdict(rec)
        d["full_text"] = rec.full_text()
        return d

    @app.get("/api/cycle-log")
    def cycle_log(limit: int = 50):
        import json as _json
        j = journal()
        rows = j.cycle_log(limit)
        # A small pool of recent intraday usage rows to attach cost by nearest ts.
        usage = j.conn.execute(
            "SELECT ts, cost_usd FROM usage WHERE cycle='intraday' "
            "ORDER BY id DESC LIMIT ?", (limit * 2,)).fetchall()
        out = []
        for r in rows:
            symbols, calls = [], []
            if r.get("tool_calls_json"):
                try:
                    calls = _json.loads(r["tool_calls_json"])
                except ValueError:
                    calls = []
            seen = set()
            for c in calls:
                sym = (c.get("input") or {}).get("symbol")
                if sym and sym.upper() not in seen:
                    seen.add(sym.upper())
                    symbols.append(sym.upper())
            cost = None
            if usage:
                cost = min(usage, key=lambda u: _ts_gap(u["ts"], r["ts"]))["cost_usd"]
            out.append({"ts": r["ts"], "summary": r["reasoning"],
                        "symbols_examined": symbols, "tool_count": len(calls),
                        "cost_usd": cost})
        return out

    @app.get("/api/signals")
    def signals(symbol: str | None = None, limit: int = 50):
        import json as _json
        rows = journal().recent_snapshots(symbol, limit)
        for r in rows:
            r["features"] = _json.loads(r["features_json"]) if r.get("features_json") else None
            r.pop("features_json", None)
        return rows

    @app.get("/api/signals/latest")
    def signals_latest():
        """One row per symbol — the newest. The signals table used to be built by
        shipping every snapshot to the browser and reducing it there."""
        import json as _json
        rows = [dict(r) for r in journal().conn.execute(
            "SELECT s.* FROM signal_snapshot s JOIN "
            "(SELECT symbol, MAX(id) AS mid FROM signal_snapshot GROUP BY symbol) m "
            "ON s.id = m.mid ORDER BY s.symbol").fetchall()]
        for r in rows:
            r["features"] = _json.loads(r["features_json"]) if r.get("features_json") else None
            r.pop("features_json", None)
        return rows

    # Whitelisted so the metric name can be interpolated into SQL safely.
    _GRID_COLS = {"last": "last", "spread_bps": "spread_bps", "atm_iv": "atm_iv",
                  "iv_rank": "iv_rank", "pc_skew": "pc_skew", "sentiment": "sentiment"}
    _GRID_FEATURES = {"momentum_20", "realized_vol", "atr_pct", "dist_from_high"}

    @app.get("/api/signals/grid")
    def signals_grid(metric: str = "momentum_20", days: int = 30, points: int = 24):
        """The heatmap, bucketed by SQLite instead of by the browser.

        Returns the shape the chart consumes directly, so the client never holds
        the raw snapshot history just to average it.
        """
        if metric in _GRID_COLS:
            expr = _GRID_COLS[metric]
        elif metric in _GRID_FEATURES:
            expr = f"json_extract(features_json, '$.{metric}')"
        else:
            raise HTTPException(400, f"unknown metric {metric!r}")
        points = max(1, min(points, 200))
        j = journal()
        window_start = datetime.now(timezone.utc) - timedelta(days=max(1, days))
        # Start at the first snapshot inside the window, not the window edge —
        # otherwise a week of history spread over a 30-day window leaves most of
        # the chart empty and squeezes the real data into the last few columns.
        first = j.conn.execute(
            "SELECT MIN(ts) AS t FROM signal_snapshot WHERE ts >= ?",
            (window_start.isoformat(),)).fetchone()["t"]
        start = window_start
        if first:
            try:
                parsed = datetime.fromisoformat(first)
                start = max(window_start, parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc))
            except ValueError:
                pass
        start_iso = start.isoformat()
        span_days = max((datetime.now(timezone.utc) - start).total_seconds() / 86400, 1 / 24)
        # Bucket index = which slice of the span the row falls in.
        width_days = span_days / points
        rows = j.conn.execute(
            f"SELECT symbol, "
            f"  CAST((julianday(ts) - julianday(?)) / ? AS INTEGER) AS b, "
            f"  AVG({expr}) AS v "
            f"FROM signal_snapshot WHERE ts >= ? AND {expr} IS NOT NULL "
            f"GROUP BY symbol, b ORDER BY symbol, b",
            (start_iso, width_days, start_iso)).fetchall()
        cells: dict[str, dict[int, float]] = {}
        for r in rows:
            b = max(0, min(points - 1, int(r["b"])))
            cells.setdefault(r["symbol"], {})[b] = r["v"]
        labels = [(start + timedelta(days=width_days * (i + .5))).isoformat()
                  for i in range(points)]
        return {
            "metric": metric, "days": days, "points": points, "cols": labels,
            "rows": [{"label": sym,
                      "cells": [{"label": labels[i], "v": cells[sym].get(i)}
                                for i in range(points)]}
                     for sym in sorted(cells)],
        }

    @app.get("/api/intel")
    def intel():
        import os
        from ..data.intel import IntelStore
        path = cfg().settings.paths.intel_db
        if not os.path.exists(path):
            return {"digest": None}
        store = IntelStore(path)
        try:
            d = store.latest_digest()
            return {"digest": d["digest_md"] if d else None,
                    "ts": d["ts"] if d else None}
        finally:
            store.close()

    @app.get("/api/news")
    def news(symbol: str | None = None, limit: int = 40):
        import os
        from ..data.intel import IntelStore
        path = cfg().settings.paths.intel_db
        if not os.path.exists(path):
            return []
        store = IntelStore(path)
        try:
            return store.recent_news(symbol, limit)
        finally:
            store.close()

    @app.get("/api/sentiment")
    def sentiment():
        import os
        from ..data.intel import IntelStore
        path = cfg().settings.paths.intel_db
        if not os.path.exists(path):
            return []
        store = IntelStore(path)
        try:
            out = []
            for sym in cfg().settings.universe.core:
                hist = store.sentiment_history(sym, days=5)
                if hist:
                    out.append({"symbol": sym, "polarity": hist[-1]["polarity"],
                                "mention_count": hist[-1]["mention_count"],
                                "ts": hist[-1]["ts"]})
            return out
        finally:
            store.close()

    @app.get("/api/watchlist")
    def watchlist():
        p = Path(cfg().settings.paths.memory_dir) / "watchlist.md"
        return {"markdown": p.read_text(encoding="utf-8") if p.exists() else None}

    @app.get("/api/query")
    def query(q: str):
        from ..analytics.decision_record import list_records
        ql = q.lower()
        hits = [r for r in list_records(journal(), limit=200)
                if ql in (r.symbol or "").lower() or ql in (r.thesis or "").lower()
                or ql in (r.strategy_tag or "").lower()]
        return [{"proposal_id": r.proposal_id, "summary": r.summary_line(),
                 "reasoning": r.reasoning} for r in hits[:20]]

    # -- price history --------------------------------------------------------

    @app.get("/api/bars/{symbol}")
    def bars(symbol: str, days: int = 180, timeframe: str = "1Day"):
        """OHLCV for the price chart. Daily bars are cached in bars.db; a cold
        cache (or any intraday request) falls through to the broker."""
        from ..data.bars import Bar, BarStore
        symbol = symbol.upper()
        daily = timeframe == "1Day"
        store = BarStore(cfg().settings.paths.bars_db)
        try:
            rows = store.load_bars(symbol) if daily else []
            if len(rows) < 5:
                fetched = _fetch_bars(broker(), symbol, days, timeframe)
                if fetched and daily:
                    store.save_bars([Bar(**b) for b in fetched])
                    rows = store.load_bars(symbol)
                else:
                    return {"symbol": symbol, "timeframe": timeframe,
                            "source": "broker" if fetched else "none", "bars": fetched}
            out = [{"date": b.date, "open": b.open, "high": b.high, "low": b.low,
                    "close": b.close, "volume": b.volume} for b in rows][-days:]
            return {"symbol": symbol, "timeframe": timeframe, "source": "cache",
                    "bars": out}
        finally:
            store.close()

    # -- observability --------------------------------------------------------

    @app.get("/api/usage")
    def usage(days: int = 14):
        """Token/cost history — what the agent spends, by cycle, agent and model."""
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        rows = [dict(r) for r in journal().conn.execute(
            "SELECT ts, cycle, agent, model, input_tokens, output_tokens, "
            "cache_read_tokens, cost_usd FROM usage WHERE ts >= ? ORDER BY ts",
            (since,)).fetchall()]
        return {"rows": rows, "total_usd": round(sum(r["cost_usd"] or 0 for r in rows), 4)}

    @app.get("/api/heartbeats")
    def heartbeats(limit: int = 300):
        rows = journal().recent_heartbeats(limit)
        return [{"ts": r["ts"], "job": r["job"], "status": r["status"],
                 "detail": r["detail"]} for r in rows]

    @app.get("/api/funnel")
    def funnel(days: int = 7):
        """The agent pipeline, stage by stage: what it looked at vs what it traded.
        The drop between two stages is the answer to 'why so few trades?'."""
        j = journal()
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        one = lambda q, a=(): j.conn.execute(q, a).fetchone()[0]  # noqa: E731
        examined = one("SELECT COUNT(DISTINCT symbol) FROM signal_snapshot WHERE ts >= ?",
                       (since,))
        proposed = one("SELECT COUNT(*) FROM proposals WHERE ts >= ?", (since,))
        vetoed = one("SELECT COUNT(DISTINCT proposal_id) FROM verdicts "
                     "WHERE verdict='veto' AND ts >= ?", (since,))
        submitted = one("SELECT COUNT(*) FROM orders WHERE ts >= ?", (since,))
        filled = one("SELECT COUNT(DISTINCT order_id) FROM fills WHERE ts >= ?", (since,))
        closed = one("SELECT COUNT(*) FROM tax_lots WHERE close_ts IS NOT NULL "
                     "AND close_ts >= ?", (since,))
        return {"days": days, "stages": [
            {"stage": "Symbols examined", "count": examined},
            {"stage": "Proposals raised", "count": proposed},
            {"stage": "Cleared guardrails", "count": max(proposed - vetoed, 0)},
            {"stage": "Orders submitted", "count": submitted},
            {"stage": "Orders filled", "count": filled},
            {"stage": "Positions closed", "count": closed},
        ]}

    @app.get("/api/verdicts")
    def verdicts(days: int = 30):
        """Which guardrail rules actually fire — the agent's real constraints."""
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        rows = [dict(r) for r in journal().conn.execute(
            "SELECT rule, verdict, COUNT(*) AS n FROM verdicts WHERE ts >= ? "
            "GROUP BY rule, verdict ORDER BY n DESC", (since,)).fetchall()]
        recent = [dict(r) for r in journal().conn.execute(
            "SELECT proposal_id, ts, source, verdict, rule, reason FROM verdicts "
            "WHERE ts >= ? ORDER BY id DESC LIMIT 100", (since,)).fetchall()]
        return {"by_rule": rows, "recent": recent}

    @app.get("/api/symbol/{symbol}")
    def symbol_detail(symbol: str):
        """Everything the system knows about one name, in one payload — the
        drill-down behind every symbol link in the UI."""
        import json as _json
        symbol = symbol.upper()
        c, j = cfg(), journal()
        state = _account_state(j)
        position = next((p.model_dump() for p in state.positions
                         if p.symbol.upper().startswith(symbol)), None) if state else None
        snaps = j.recent_snapshots(symbol, 200)
        for s in snaps:
            s["features"] = _json.loads(s["features_json"]) if s.get("features_json") else None
            s.pop("features_json", None)
        proposals = [dict(r) for r in j.conn.execute(
            "SELECT id, ts, strategy_tag, side, qty, status, thesis, confidence, "
            "expected_edge_usd FROM proposals WHERE symbol=? ORDER BY id DESC LIMIT 25",
            (symbol,)).fetchall()]
        orders = [dict(r) for r in j.conn.execute(
            "SELECT id, ts, side, qty, order_type, limit_price, status FROM orders "
            "WHERE symbol=? ORDER BY id DESC LIMIT 25", (symbol,)).fetchall()]
        lots = [dict(r) for r in j.conn.execute(
            "SELECT id, qty, open_ts, open_price, close_ts, close_price, realized_pnl, "
            "term, strategy_tag FROM tax_lots WHERE symbol=? ORDER BY id DESC LIMIT 25",
            (symbol,)).fetchall()]
        news = []
        import os
        if os.path.exists(c.settings.paths.intel_db):
            from ..data.intel import IntelStore
            store = IntelStore(c.settings.paths.intel_db)
            try:
                news = store.recent_news(symbol, 20)
            finally:
                store.close()
        return {"symbol": symbol, "position": position, "snapshots": snaps,
                "proposals": proposals, "orders": orders, "lots": lots, "news": news}

    # -- config ---------------------------------------------------------------

    @app.get("/api/config")
    def config_view():
        c = cfg()
        return {"limits": c.limits.model_dump(mode="json"),
                "settings": c.settings.model_dump(mode="json")}

    @app.get("/api/config/schema")
    def config_schema():
        """Pydantic already knows every field's type, bound and default — hand that
        to the UI so the config editor is a real form, not a JSON textarea."""
        from ..config import PROJECT_ROOT
        return {
            "limits": {"schema": Limits.model_json_schema(),
                       "help": _yaml_help(PROJECT_ROOT / "config" / "limits.yaml")},
            "settings": {"schema": Settings.model_json_schema(),
                         "help": _yaml_help(PROJECT_ROOT / "config" / "settings.yaml")},
        }

    @app.post("/api/config/validate")
    def config_validate(payload: dict):
        model = {"limits": Limits, "settings": Settings}.get(payload.get("section"))
        if model is None:
            raise HTTPException(400, "section must be 'limits' or 'settings'")
        try:
            model.model_validate(payload.get("data", {}))
            return {"ok": True, "errors": []}
        except Exception as e:
            return {"ok": False, "errors": str(e)}

    @app.post("/api/config/save")
    def config_save(payload: dict):
        import yaml
        section = payload.get("section")
        model = {"limits": Limits, "settings": Settings}.get(section)
        if model is None:
            raise HTTPException(400, "section must be 'limits' or 'settings'")
        try:
            model.model_validate(payload.get("data", {}))
        except Exception as e:
            return {"ok": False, "errors": str(e)}
        from ..config import PROJECT_ROOT
        fname = "limits.yaml" if section == "limits" else "settings.yaml"
        path = PROJECT_ROOT / "config" / fname
        # Snapshot before overwrite — these files govern real money limits.
        backup = None
        if path.exists():
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            bdir = Path(cfg().settings.paths.journal_db).parent / "backups" / "config"
            bdir.mkdir(parents=True, exist_ok=True)
            backup = bdir / f"{path.stem}.{stamp}.yaml"
            backup.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
        path.write_text(yaml.safe_dump(payload["data"], sort_keys=False), encoding="utf-8")
        get_config.cache_clear()
        return {"ok": True, "saved": str(path),
                "backup": str(backup) if backup else None}

    # -- human actions (localhost-only) --------------------------------------

    @app.post("/api/actions/reset-kill-switch")
    def reset_kill_switch():
        journal().reset_kill_switch()
        return {"ok": True}

    @app.post("/api/actions/approve/{proposal_id}")
    def approve(proposal_id: int):
        from ..guardrails.engine import OrderPipeline
        config = cfg()
        j = journal()
        pipeline = OrderPipeline(config, j, broker())
        try:
            res = pipeline.approve(proposal_id)
            return {"ok": True, "status": res.status}
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.post("/api/actions/deny/{proposal_id}")
    def deny(proposal_id: int):
        from ..approvals import handle_approval_command
        j = journal()
        reply = handle_approval_command(None, j, f"deny {proposal_id}")
        return {"ok": True, "message": reply}

    @app.post("/api/actions/run-scoring")
    def run_scoring():
        """Grade what is gradable now, without waiting for the end-of-day cycle:
        score closed lots, then counterfactual-grade aged declined proposals.
        Deterministic and cheap — no model calls."""
        from ..analytics.counterfactuals import evaluate_pending
        from ..analytics.scorer import score_closed_trades
        j = journal()
        scored = score_closed_trades(j)
        cf_evaluated = cf_right = cf_skipped = 0
        error = None
        try:
            cf = evaluate_pending(j, broker())
            cf_evaluated, cf_right, cf_skipped = cf.evaluated, cf.right, cf.skipped
        except Exception as e:
            error = str(e)[:200]
        return {"ok": True, "scored": scored.scored, "gross_pnl": scored.gross_pnl,
                "graded": cf_evaluated, "graded_right": cf_right,
                "skipped": cf_skipped, "error": error}

    @app.post("/api/actions/run-cycle")
    def run_cycle(payload: dict):
        import threading
        cycle = (payload or {}).get("cycle", "premarket")

        def _run():
            try:
                from ..agents.client import make_client
                from ..orchestrator import Orchestrator
                config = cfg()
                j = journal()
                b = broker()
                orch = Orchestrator(config, j, b, make_client(config))
                orch.run_cycle(cycle)
            except Exception:
                pass

        threading.Thread(target=_run, daemon=True).start()
        return {"ok": True, "started": cycle}

    # -- frontend -------------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    def index():
        idx = _STATIC / "index.html"
        if idx.exists():
            return FileResponse(str(idx))
        return HTMLResponse("<h1>dashboard static file missing</h1>", status_code=500)

    from fastapi.staticfiles import StaticFiles
    app.mount("/static", StaticFiles(directory=str(_STATIC)), name="static")

    return app

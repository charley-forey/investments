"""Operations dashboard (FastAPI, localhost-only) — the full transparency + control
cockpit. Every read endpoint reuses existing analytics/journal/broker; the dashboard
adds views and human-triggered actions, never new authority. Order execution still
flows only through the guardrail pipeline.

Trust boundary: bind to 127.0.0.1. The action and config-save endpoints are powerful
and intentionally unauthenticated for single-user local use (remote/multi-user auth
is an M13 blocked item).
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


def create_app(get_config_fn=get_config, broker_factory=None):
    from fastapi import FastAPI, HTTPException
    from fastapi.responses import FileResponse, HTMLResponse

    app = FastAPI(title="Agentic Trading — Operations")
    _make_broker = broker_factory or _default_broker_factory

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
        j = journal()
        orders = [dict(o) for o in j.conn.execute(
            "SELECT * FROM orders ORDER BY id DESC LIMIT ?", (limit,)).fetchall()]
        for o in orders:
            o["fills"] = [dict(f) for f in j.conn.execute(
                "SELECT * FROM fills WHERE order_id=? ORDER BY id", (o["id"],)).fetchall()]
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

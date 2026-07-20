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

    # -- config ---------------------------------------------------------------

    @app.get("/api/config")
    def config_view():
        c = cfg()
        return {"limits": c.limits.model_dump(mode="json"),
                "settings": c.settings.model_dump(mode="json")}

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
        path.write_text(yaml.safe_dump(payload["data"], sort_keys=False), encoding="utf-8")
        get_config.cache_clear()
        return {"ok": True, "saved": str(path)}

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

    return app

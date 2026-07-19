"""Local observability dashboard (FastAPI, localhost-only).

Exposes the system's full transparency surface: live metrics, the Decision Records
WITH the agents' captured reasoning, per-strategy performance, allocations, the
market-intel digest, and a human-only config view/edit. Every endpoint reads
existing analytics — the dashboard adds visibility, not new authority. No LLM path
can write config; config edits validate against the pydantic models before writing.
"""

from __future__ import annotations

from dataclasses import asdict

from ..config import Limits, Settings, get_config
from ..data.journal import Journal


def create_app(get_config_fn=get_config):
    from fastapi import FastAPI, HTTPException
    from fastapi.responses import HTMLResponse

    app = FastAPI(title="Agentic Trading — Observability")

    def cfg():
        return get_config_fn()

    def _journal() -> Journal:
        return Journal(cfg().settings.paths.journal_db)

    @app.get("/api/metrics")
    def metrics():
        from ..monitoring import metrics_snapshot
        return metrics_snapshot(cfg(), _journal())

    @app.get("/api/decisions")
    def decisions(limit: int = 25):
        from ..analytics.decision_record import list_records
        return [{"proposal_id": r.proposal_id, "ts": r.ts, "strategy_tag": r.strategy_tag,
                 "symbol": r.symbol, "side": r.side, "qty": r.qty, "status": r.status,
                 "thesis": r.thesis} for r in list_records(_journal(), limit)]

    @app.get("/api/decisions/{proposal_id}")
    def decision(proposal_id: int):
        from ..analytics.decision_record import build_record
        rec = build_record(_journal(), proposal_id)
        if rec is None:
            raise HTTPException(404, "no such decision")
        d = asdict(rec)
        d["full_text"] = rec.full_text()
        return d

    @app.get("/api/performance")
    def performance():
        from ..analytics.allocation import allocate_capital
        from ..analytics.stats import stats_by_tag
        config = cfg()
        journal = _journal()
        stats = {t: asdict(s) for t, s in stats_by_tag(journal, config.settings.tax).items()}
        allocs = [asdict(a) for a in allocate_capital(journal, config.settings.tax)]
        return {"per_strategy": stats, "allocation": allocs}

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
            return {"digest": d["digest_md"] if d else None}
        finally:
            store.close()

    @app.get("/api/query")
    def query(q: str):
        """Retrieval over recent decision records by symbol/keyword — 'why did you
        do X'. Deterministic match; an LLM synthesis can layer on top later."""
        from ..analytics.decision_record import list_records
        ql = q.lower()
        hits = [r for r in list_records(_journal(), limit=200)
                if ql in (r.symbol or "").lower()
                or ql in (r.thesis or "").lower()
                or ql in (r.strategy_tag or "").lower()]
        return [{"proposal_id": r.proposal_id, "symbol": r.symbol, "status": r.status,
                 "summary": r.summary_line(), "reasoning": r.reasoning} for r in hits[:20]]

    @app.get("/api/config")
    def config_view():
        config = cfg()
        return {"limits": config.limits.model_dump(), "settings": config.settings.model_dump()}

    @app.post("/api/config/validate")
    def config_validate(payload: dict):
        """Validate a proposed config section against its pydantic model without
        writing. Returns {ok, errors}. (Human-only; the write endpoint is separate
        and intentionally left to the operator to wire with auth for their deploy.)"""
        section = payload.get("section")
        data = payload.get("data", {})
        model = {"limits": Limits, "settings": Settings}.get(section)
        if model is None:
            raise HTTPException(400, "section must be 'limits' or 'settings'")
        try:
            model.model_validate(data)
            return {"ok": True, "errors": []}
        except Exception as e:  # pydantic ValidationError
            return {"ok": False, "errors": str(e)}

    @app.get("/", response_class=HTMLResponse)
    def index():
        return _INDEX_HTML

    return app


_INDEX_HTML = """<!doctype html><html><head><meta charset=utf-8>
<title>Agentic Trading — Observability</title>
<style>
 body{font:14px system-ui;margin:0;background:#0f1115;color:#e6e6e6}
 header{padding:12px 20px;background:#161a20;font-weight:600}
 .grid{display:grid;grid-template-columns:1fr 1fr;gap:16px;padding:20px}
 .card{background:#161a20;border:1px solid #222;border-radius:8px;padding:16px}
 h3{margin:0 0 10px} pre{white-space:pre-wrap;font:12px ui-monospace;color:#cbd}
 a{color:#7aa2f7;cursor:pointer}
</style></head><body>
<header>Agentic Trading — Observability</header>
<div class=grid>
 <div class=card><h3>System metrics</h3><pre id=metrics>loading…</pre></div>
 <div class=card><h3>Market intelligence</h3><pre id=intel>loading…</pre></div>
 <div class=card><h3>Recent decisions</h3><pre id=decisions>loading…</pre>
   <div>Inspect: <input id=pid size=6><a onclick=loadWhy()>why?</a></div>
   <pre id=why></pre></div>
 <div class=card><h3>Performance</h3><pre id=perf>loading…</pre></div>
</div>
<script>
async function j(u){return (await fetch(u)).json()}
async function load(){
 document.getElementById('metrics').textContent=JSON.stringify(await j('/api/metrics'),null,2);
 const intel=await j('/api/intel');document.getElementById('intel').textContent=intel.digest||'no digest yet';
 const d=await j('/api/decisions');document.getElementById('decisions').textContent=
   d.map(x=>`#${x.proposal_id} ${x.strategy_tag} ${x.side} ${x.qty} ${x.symbol} -> ${x.status}`).join('\\n')||'none';
 const p=await j('/api/performance');document.getElementById('perf').textContent=JSON.stringify(p,null,2);
}
async function loadWhy(){const id=document.getElementById('pid').value;
 if(!id)return;const r=await j('/api/decisions/'+id);
 document.getElementById('why').textContent=r.full_text||JSON.stringify(r,null,2);}
load();setInterval(load,15000);
</script></body></html>"""

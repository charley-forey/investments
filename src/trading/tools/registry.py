"""Tool schemas + dispatcher for the agents.

Read-only tools return compact text summaries. `propose_order` only registers a
draft on the context — execution belongs to the orchestrator, behind the risk
agent and the guardrail pipeline. Tool failures return error text (never raise)
so agents can adapt.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ..broker.models import AccountState
from ..config import Config
from ..data.journal import Journal
from ..guardrails.account_math import account_snapshot_summary
from ..guardrails.models import OptionLeg, OrderProposal


@dataclass
class ToolContext:
    config: Config
    journal: Journal
    broker: object            # AlpacaBroker or a stub in tests
    account_state: AccountState
    agent_name: str = "agent"
    drafts: list[OrderProposal] = field(default_factory=list)


LEG_SCHEMA = {
    "type": "object",
    "properties": {
        "side": {"type": "string", "enum": ["buy", "sell"]},
        "right": {"type": "string", "enum": ["call", "put"]},
        "strike": {"type": "number"},
        "expiry": {"type": "string", "description": "YYYY-MM-DD"},
        "qty": {"type": "integer", "minimum": 1},
        "est_premium": {"type": "number", "description": "per-share premium estimate"},
    },
    "required": ["side", "right", "strike", "expiry", "qty", "est_premium"],
}

TOOL_SCHEMAS: dict[str, dict] = {
    "get_account_state": {
        "description": "Current account snapshot: equity, cash, buying power, daily P&L, "
                       "day-trade count, open positions, and tax lots with holding periods. "
                       "Call this before proposing any trade.",
        "input_schema": {"type": "object", "properties": {}},
    },
    "get_quote": {
        "description": "Latest bid/ask/mid/spread for a stock symbol.",
        "input_schema": {
            "type": "object",
            "properties": {"symbol": {"type": "string"}},
            "required": ["symbol"],
        },
    },
    "get_bars": {
        "description": "Recent daily OHLCV bars for a symbol (compact table).",
        "input_schema": {
            "type": "object",
            "properties": {"symbol": {"type": "string"}},
            "required": ["symbol"],
        },
    },
    "get_options_chain": {
        "description": "Trimmed options chain for an underlying: near-the-money strikes "
                       "within the configured DTE window.",
        "input_schema": {
            "type": "object",
            "properties": {"symbol": {"type": "string"}},
            "required": ["symbol"],
        },
    },
    "search_news": {
        "description": "Recent news headlines for a symbol. Call this when the answer depends "
                       "on current events or a thesis references news.",
        "input_schema": {
            "type": "object",
            "properties": {"symbol": {"type": "string"}},
            "required": ["symbol"],
        },
    },
    "read_journal": {
        "description": "Recent trade proposals with their verdicts and outcomes — use it to "
                       "avoid repeating recent mistakes or duplicating open theses.",
        "input_schema": {
            "type": "object",
            "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 50}},
        },
    },
    "read_memory": {
        "description": "Read the agent memory files (lessons, watchlist, playbook notes). "
                       "Call this at the start of a session.",
        "input_schema": {"type": "object", "properties": {}},
    },
    "read_playbook": {
        "description": "Read the strategy playbooks (rules, filters, known failure modes "
                       "per strategy tag). Consult before trading a tagged setup.",
        "input_schema": {"type": "object", "properties": {}},
    },
    "get_sentiment": {
        "description": "Crude sentiment signal for a symbol: news + Reddit mention volume "
                       "and a headline polarity lean. A signal to weigh, not a decision.",
        "input_schema": {
            "type": "object",
            "properties": {"symbol": {"type": "string"}},
            "required": ["symbol"],
        },
    },
    "get_market_context": {
        "description": "Broad-market regime read (SPY trend + realized volatility). Check it "
                       "before proposing directional trades — a name's setup means less "
                       "when the tape disagrees.",
        "input_schema": {"type": "object", "properties": {}},
    },
    "get_features": {
        "description": "Computed technical features for a symbol (momentum, SMA gap, "
                       "realized vol, ATR%, distance from high) — a consistent feature set "
                       "shared with backtests.",
        "input_schema": {
            "type": "object",
            "properties": {"symbol": {"type": "string"}},
            "required": ["symbol"],
        },
    },
    "get_calendar": {
        "description": "Upcoming scheduled events (earnings, economic releases) for a symbol "
                       "from the configured calendar feed. Use it for event-risk awareness — "
                       "avoid holding through a binary event unless that's the thesis.",
        "input_schema": {
            "type": "object",
            "properties": {"symbol": {"type": "string"}},
        },
    },
    "get_market_intel": {
        "description": "The latest curated market-intelligence digest (what's moving the "
                       "market and why, from continuously ingested news + social).",
        "input_schema": {"type": "object", "properties": {}},
    },
    "recall_similar": {
        "description": "Semantic recall: retrieve past news, lessons, or decisions similar "
                       "to a query — 'have we seen this setup before, and what happened?'",
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    },
    "propose_order": {
        "description": "Register a trade proposal for independent risk review. It is NOT "
                       "executed by this call. Every proposal needs a falsifiable thesis, "
                       "an expected edge in USD, and (for stocks) a stop price.",
        "input_schema": {
            "type": "object",
            "properties": {
                "symbol": {"type": "string"},
                "asset_class": {"type": "string", "enum": ["stock", "option"]},
                "side": {"type": "string", "enum": ["buy", "sell"]},
                "qty": {"type": "number", "minimum": 0},
                "limit_price": {"type": "number"},
                "stop_price": {"type": "number"},
                "target_price": {"type": "number", "description": "take-profit for the bracket"},
                "legs": {"type": "array", "items": LEG_SCHEMA},
                "thesis": {"type": "string"},
                "expected_edge_usd": {"type": "number"},
                "strategy_tag": {"type": "string"},
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                "reduces_position": {"type": "boolean"},
            },
            "required": ["symbol", "asset_class", "side", "thesis",
                         "expected_edge_usd", "strategy_tag"],
        },
    },
}

READ_ONLY_TOOLS = [t for t in sorted(TOOL_SCHEMAS) if t != "propose_order"]
STRATEGY_TOOLS = sorted(TOOL_SCHEMAS)


class ToolRegistry:
    def __init__(self, ctx: ToolContext, allowed: list[str]):
        unknown = set(allowed) - set(TOOL_SCHEMAS)
        if unknown:
            raise ValueError(f"unknown tools: {unknown}")
        self.ctx = ctx
        self.allowed = sorted(allowed)  # deterministic order -> stable prompt cache

    def schemas(self) -> list[dict]:
        return [
            {"name": name, **TOOL_SCHEMAS[name]}
            for name in self.allowed
        ]

    def dispatch(self, name: str, tool_input: dict) -> str:
        if name not in self.allowed:
            return f"error: tool '{name}' not available to this agent"
        handler = getattr(self, f"_t_{name}", None)
        if handler is None:
            return f"error: no handler for tool '{name}'"
        try:
            return handler(tool_input or {})
        except Exception as e:  # tool errors are data for the agent, not crashes
            return f"error: {type(e).__name__}: {e}"

    # -- handlers -------------------------------------------------------------

    def _t_get_account_state(self, _inp: dict) -> str:
        return account_snapshot_summary(self.ctx.account_state)

    def _t_get_quote(self, inp: dict) -> str:
        q = self.ctx.broker.get_quote(inp["symbol"])
        return (f"{q.symbol}: bid {q.bid:.2f} ask {q.ask:.2f} mid {q.mid:.2f} "
                f"spread {q.spread:.2f}")

    def _t_get_bars(self, inp: dict) -> str:
        days = self.ctx.config.settings.agents.bars_lookback_days
        df = self.ctx.broker.get_bars(inp["symbol"], days=days)
        if df is None or len(df) == 0:
            return f"no bars for {inp['symbol']}"
        rows = ["date        open     high     low      close    volume"]
        for idx, row in df.tail(days).iterrows():
            ts = idx[1] if isinstance(idx, tuple) else idx
            date_s = str(ts)[:10]
            rows.append(
                f"{date_s}  {row['open']:<8.2f} {row['high']:<8.2f} "
                f"{row['low']:<8.2f} {row['close']:<8.2f} {int(row['volume'])}"
            )
        return "\n".join(rows)

    def _t_get_options_chain(self, inp: dict) -> str:
        from datetime import date

        from ..broker.occ import parse_occ

        settings = self.ctx.config.settings.agents
        symbol = inp["symbol"].upper()
        chain = self.ctx.broker.get_options_chain(symbol)
        spot = self.ctx.broker.get_quote(symbol).mid
        if not chain:
            return f"no options chain for {symbol}"

        # chain: dict of occ_symbol -> snapshot. Trim to near-the-money, DTE window.
        rows = []
        for occ, snap in chain.items():
            try:
                parts = parse_occ(occ, underlying=symbol)
            except ValueError:
                continue
            exp, right, strike = parts.expiry, parts.right, parts.strike
            dte = (exp - date.today()).days
            if dte < 0 or dte > settings.options_chain_max_dte:
                continue
            if spot > 0 and abs(strike - spot) / spot > 0.10:
                continue
            quote = getattr(snap, "latest_quote", None)
            bid = float(getattr(quote, "bid_price", 0) or 0)
            ask = float(getattr(quote, "ask_price", 0) or 0)
            rows.append((exp, right, strike, bid, ask, occ))

        rows.sort()
        if not rows:
            return f"no near-the-money contracts within {settings.options_chain_max_dte} DTE for {symbol}"
        out = [f"{symbol} spot ~{spot:.2f} — near-the-money chain (exp right strike bid ask occ)"]
        for exp, right, strike, bid, ask, occ in rows[:80]:
            out.append(f"{exp} {right:<4} {strike:<8.2f} {bid:<6.2f} {ask:<6.2f} {occ}")
        return "\n".join(out)

    def _t_search_news(self, inp: dict) -> str:
        limit = self.ctx.config.settings.agents.news_limit
        items = self.ctx.broker.get_news(inp["symbol"], limit=limit)
        if not items:
            return f"no recent news for {inp['symbol']}"
        out = []
        for it in items:
            out.append(f"[{it['created_at'][:10]}] {it['headline']} ({it['source']})")
            if it.get("summary"):
                out.append(f"    {it['summary']}")
        return "\n".join(out)

    def _t_read_journal(self, inp: dict) -> str:
        limit = int(inp.get("limit", 20))
        rows = self.ctx.journal.conn.execute(
            "SELECT * FROM proposals ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        if not rows:
            return "journal is empty — no prior proposals"
        out = []
        for r in rows:
            verdicts = self.ctx.journal.verdicts_for(r["id"])
            vs = "; ".join(f"{v['source']}:{v['verdict']}"
                           + (f"[{v['rule']}]" if v["rule"] else "") for v in verdicts)
            out.append(
                f"#{r['id']} {r['ts'][:16]} {r['strategy_tag']} {r['side']} "
                f"{r['qty']:g} {r['symbol']} ({r['asset_class']}) -> {r['status']} | {vs}"
            )
        return "\n".join(out)

    def _t_read_memory(self, _inp: dict) -> str:
        return self._read_dir(self.ctx.config.settings.paths.memory_dir, "memory")

    def _t_read_playbook(self, _inp: dict) -> str:
        return self._read_dir(self.ctx.config.settings.paths.playbooks_dir, "playbook")

    @staticmethod
    def _read_dir(dir_path: str, label: str) -> str:
        d = Path(dir_path)
        if not d.exists():
            return f"no {label} files yet"
        chunks = []
        for f in sorted(d.glob("*.md")):
            chunks.append(f"=== {f.name} ===\n{f.read_text(encoding='utf-8').strip()}")
        return "\n\n".join(chunks) or f"no {label} files yet"

    def _t_get_sentiment(self, inp: dict) -> str:
        from ..data.sentiment import get_symbol_sentiment

        signal = get_symbol_sentiment(self.ctx.config, self.ctx.broker, inp["symbol"])
        return signal.summary()

    def _t_get_market_context(self, _inp: dict) -> str:
        from .market_context import market_regime

        return market_regime(self.ctx.broker).summary()

    def _t_get_market_intel(self, _inp: dict) -> str:
        import os

        from ..data.intel import IntelStore

        path = self.ctx.config.settings.paths.intel_db
        if not os.path.exists(path):
            return "no market-intel digest yet"
        store = IntelStore(path)
        try:
            d = store.latest_digest()
            return d["digest_md"] if d else "no market-intel digest yet"
        finally:
            store.close()

    def _t_recall_similar(self, inp: dict) -> str:
        import os

        from ..data.vectors import VectorStore

        path = self.ctx.config.settings.paths.vectors_db
        if not os.path.exists(path):
            return "no semantic memory yet"
        store = VectorStore(path)
        try:
            hits = store.search(inp["query"], k=5)
            if not hits:
                return "no similar prior context found"
            return "\n".join(f"[{h.kind} {h.score:.2f}] {h.text[:200]}" for h in hits)
        finally:
            store.close()

    def _t_get_features(self, inp: dict) -> str:
        from ..analytics.features import compute_features

        days = self.ctx.config.settings.agents.bars_lookback_days
        df = self.ctx.broker.get_bars(inp["symbol"], days=max(days, 40))
        if df is None or len(df) == 0:
            return f"no bars for {inp['symbol']}"
        rows = [type("B", (), {"open": float(r["open"]), "high": float(r["high"]),
                               "low": float(r["low"]), "close": float(r["close"])})()
                for _, r in df.iterrows()]
        feats = compute_features(rows)
        return feats.summary() if feats else f"insufficient history for {inp['symbol']}"

    def _t_get_calendar(self, inp: dict) -> str:
        import json
        from datetime import date
        from pathlib import Path

        path = Path(self.ctx.config.settings.paths.calendar_file)
        if not path.exists():
            return "no calendar feed configured (data/calendar.json)"
        try:
            events = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return "calendar feed is unreadable"
        today = date.today().isoformat()
        sym = (inp.get("symbol") or "").upper()
        upcoming = [e for e in events if e.get("date", "") >= today
                    and (not sym or e.get("symbol", "").upper() == sym)]
        upcoming.sort(key=lambda e: e.get("date", ""))
        if not upcoming:
            return f"no upcoming events{(' for ' + sym) if sym else ''}"
        return "\n".join(f"{e['date']} {e.get('symbol', '')} {e.get('event', '')}"
                         for e in upcoming[:20])

    def _t_propose_order(self, inp: dict) -> str:
        max_props = self.ctx.config.settings.agents.max_proposals_per_cycle
        if len(self.ctx.drafts) >= max_props:
            return f"error: proposal budget for this cycle ({max_props}) already used"
        legs = [OptionLeg(**leg) for leg in inp.get("legs", [])]
        proposal = OrderProposal(
            agent=self.ctx.agent_name,
            strategy_tag=inp["strategy_tag"],
            symbol=inp["symbol"],
            asset_class=inp["asset_class"],
            side=inp.get("side", "buy"),
            qty=float(inp.get("qty", 0)),
            order_type="limit",
            limit_price=inp.get("limit_price"),
            stop_price=inp.get("stop_price"),
            target_price=inp.get("target_price"),
            legs=legs,
            thesis=inp["thesis"],
            expected_edge_usd=inp["expected_edge_usd"],
            confidence=inp.get("confidence"),
            reduces_position=bool(inp.get("reduces_position", False)),
        )
        if proposal.asset_class == "stock":
            if proposal.qty <= 0:
                return "error: stock proposal needs qty > 0"
            if proposal.limit_price is None:
                return "error: stock proposal needs limit_price"
            if not proposal.reduces_position and proposal.stop_price is None:
                return "error: opening stock proposal needs stop_price (used for sizing/risk)"
        elif not proposal.legs:
            return "error: option proposal needs legs"
        self.ctx.drafts.append(proposal)
        return (f"draft #{len(self.ctx.drafts)} registered for risk review: "
                f"{proposal.side} {proposal.qty:g} {proposal.symbol} "
                f"({proposal.strategy_tag}). It will NOT execute unless the risk agent "
                f"and guardrails approve.")

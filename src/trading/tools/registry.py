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
    "get_intraday_features": {
        "description": "Intraday microstructure for a symbol from today's session: VWAP, "
                       "opening-range high/low and breakout, intraday momentum, and relative "
                       "volume. Use it to time entries within the day — a daily setup still "
                       "needs an intraday trigger.",
        "input_schema": {
            "type": "object",
            "properties": {"symbol": {"type": "string"}},
            "required": ["symbol"],
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
    "scan_universe": {
        "description": "Batch screener across the configured universe: one ranked table of "
                       "last price, day %, gap %, RVOL, ATR%, momentum, SMA distance. Use "
                       "this instead of calling get_features on every symbol.",
        "input_schema": {
            "type": "object",
            "properties": {
                "sort_by": {
                    "type": "string",
                    "enum": ["momentum", "day_pct", "gap_pct", "rvol", "atr_pct", "sma_gap"],
                    "description": "Column to rank by (default: momentum)",
                },
            },
        },
    },
    "get_fundamentals": {
        "description": "Fundamental snapshot for a symbol: market cap, trailing/forward P/E, "
                       "revenue growth, profit margin, short % of float, beta, next earnings. "
                       "Cached daily from Yahoo Finance.",
        "input_schema": {
            "type": "object",
            "properties": {"symbol": {"type": "string"}},
            "required": ["symbol"],
        },
    },
    "get_open_orders": {
        "description": "List pending/unfilled broker orders so you do not double-propose "
                       "against a resting limit. Call before proposing a new entry in a "
                       "symbol that may already have working orders.",
        "input_schema": {"type": "object", "properties": {}},
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
    "propose_vertical": {
        "description": "Express a DIRECTIONAL view as a defined-risk debit vertical spread in "
                       "ONE call: code picks the two strikes and expiry from the live chain and "
                       "sizes the position under the options max-loss cap, so you cannot fumble "
                       "the legs. direction='bullish' builds a debit CALL spread; 'bearish' a "
                       "debit PUT spread. Prefer this over a naked directional STOCK trade into a "
                       "binary/catalyst — the stock version gets vetoed for overnight gap risk, "
                       "this caps the loss at the net debit. For non-vertical structures (credit "
                       "spreads, CSP, covered calls) hand-build legs with propose_order instead.",
        "input_schema": {
            "type": "object",
            "properties": {
                "symbol": {"type": "string"},
                "direction": {"type": "string", "enum": ["bullish", "bearish"]},
                "structure": {"type": "string", "enum": ["debit", "credit"],
                              "description": "debit = buy premium (directional, cheap IV); "
                                             "credit = sell premium (rich IV, capped max loss). "
                                             "Default debit."},
                "thesis": {"type": "string"},
                "expected_edge_usd": {"type": "number"},
                "max_loss_usd": {"type": "number",
                                 "description": "risk budget; capped at the options max-loss limit"},
                "target_dte": {"type": "integer",
                               "description": "preferred days to expiry (e.g. past an earnings date)"},
                "strategy_tag": {"type": "string"},
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            },
            "required": ["symbol", "direction", "thesis", "expected_edge_usd"],
        },
    },
}

_PROPOSE_TOOLS = ("propose_order", "propose_vertical")
READ_ONLY_TOOLS = [t for t in sorted(TOOL_SCHEMAS) if t not in _PROPOSE_TOOLS]
STRATEGY_TOOLS = sorted(TOOL_SCHEMAS)


def _bars_shim(df):
    """Adapt a bars DataFrame into the .open/.high/.low/.close/.volume row shape that
    compute_features and compute_intraday_features expect."""
    def _vol(r):
        try:
            return float(r["volume"])
        except (KeyError, TypeError, ValueError):
            return 0.0
    return [type("B", (), {"open": float(r["open"]), "high": float(r["high"]),
                           "low": float(r["low"]), "close": float(r["close"]),
                           "volume": _vol(r)})()
            for _, r in df.iterrows()]


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
        mid = q.mid
        spread_pct = (100.0 * q.spread / mid) if mid > 0 else 0.0
        line = (f"{q.symbol}: bid {q.bid:.2f} ask {q.ask:.2f} mid {mid:.2f} "
                f"spread {q.spread:.2f} ({spread_pct:.2f}%)")
        # Wide spreads are often stale/one-sided snapshots — flag them so the
        # agent does not build a thesis on an untradeable book.
        if mid > 0 and spread_pct > 1.0:
            line += (f"\nWARNING: spread {spread_pct:.1f}% of mid looks stale/unreliable "
                     f"— do not size or enter on this quote; wait for a tight book "
                     f"(<0.4%) or confirm with a fresh last trade.")
        return line

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
        from ..analytics.options import chain_rows, chain_signals, iv_rank
        from ..analytics.features import compute_features

        settings = self.ctx.config.settings.agents
        symbol = inp["symbol"].upper()
        chain = self.ctx.broker.get_options_chain(symbol)
        spot = self.ctx.broker.get_quote(symbol).mid
        if not chain:
            return f"no options chain for {symbol}"

        min_dte = self.ctx.config.limits.options.min_days_to_expiry
        rows = chain_rows(chain, symbol, spot, max_dte=settings.options_chain_max_dte,
                          min_dte=min_dte)
        if not rows:
            return f"no near-the-money contracts within {settings.options_chain_max_dte} DTE for {symbol}"

        # Realized vol for an IV-vs-RV read (best-effort).
        realized_vol = None
        try:
            df = self.ctx.broker.get_bars(symbol, days=max(settings.bars_lookback_days, 40))
            if df is not None and len(df):
                feats = compute_features(_bars_shim(df))
                realized_vol = feats.realized_vol if feats else None
        except Exception:
            pass

        sig = chain_signals(rows, realized_vol)
        rank = iv_rank(sig.atm_iv, self._atm_iv_history(symbol))
        head = f"{symbol} spot ~{spot:.2f} — {sig.summary()}"
        if rank is not None:
            head += f"; IV rank {rank:.0f}%"
        head += ("\nStructure hints: buy premium (long/debit) when IV is cheap & you have a "
                 "dated catalyst; sell premium (credit vertical / CSP / covered call) when IV "
                 "is rich. Watch theta (decay), delta (leverage), and the spread (liquidity).")
        # Deterministic vol-premium read from IV rank + regime + event window.
        try:
            from ..data.calendar_provider import get_calendar_provider
            from ..scanner.vol_premium import describe_suggestion, suggest_vol_structure
            from .market_context import market_regime
            trend = market_regime(self.ctx.broker).trend
            events = get_calendar_provider(self.ctx.config).upcoming_events(
                symbol, days=settings.options_chain_max_dte)
            hint = describe_suggestion(suggest_vol_structure(rank, trend, bool(events)), symbol)
            if hint:
                head += f"\n{hint}"
        except Exception:
            pass
        lines = [head, "exp right strike bid ask iv delta theta vega dte occ"]
        for r in rows[:80]:
            lines.append(r.line())
        return "\n".join(lines)

    def _atm_iv_history(self, symbol: str) -> list:
        """Stored ATM IV history for IV rank (from the per-interval snapshot dataset)."""
        try:
            rows = self.ctx.journal.conn.execute(
                "SELECT atm_iv FROM signal_snapshot WHERE symbol=? AND atm_iv IS NOT NULL "
                "ORDER BY id DESC LIMIT 200", (symbol.upper(),)).fetchall()
            return [r["atm_iv"] for r in rows]
        except Exception:
            return []

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
        feats = compute_features(_bars_shim(df))
        return feats.summary() if feats else f"insufficient history for {inp['symbol']}"

    def _t_get_intraday_features(self, inp: dict) -> str:
        from ..analytics.intraday import compute_intraday_features

        agents = self.ctx.config.settings.agents
        tf = getattr(agents, "intraday_timeframe", "5Min")
        try:
            df = self.ctx.broker.get_bars(inp["symbol"], days=1, timeframe=tf)
        except TypeError:
            df = self.ctx.broker.get_bars(inp["symbol"], days=1)  # broker without timeframe
        if df is None or len(df) == 0:
            return f"no intraday bars for {inp['symbol']}"
        feats = compute_intraday_features(
            _bars_shim(df), or_bars=getattr(agents, "opening_range_bars", 6))
        return feats.summary() if feats else f"insufficient intraday history for {inp['symbol']}"

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
        upcoming = []
        for e in events:
            if e.get("date", "") < today:
                continue
            e_sym = (e.get("symbol") or "").upper()
            is_macro = (not e_sym) and (
                e.get("event_type") == "macro"
                or str(e.get("event", "")).upper() in {
                    "FOMC", "CPI", "NFP", "PCE", "GDP",
                }
            )
            if not sym or e_sym == sym or is_macro:
                upcoming.append(e)
        upcoming.sort(key=lambda e: e.get("date", ""))
        if not upcoming:
            return f"no upcoming events{(' for ' + sym) if sym else ''}"
        lines = []
        for e in upcoming[:30]:
            et = e.get("event_type") or ("macro" if not e.get("symbol") else "event")
            sym_s = e.get("symbol") or "MARKET"
            lines.append(f"{e['date']} [{et}] {sym_s} {e.get('event', '')}")
        return "\n".join(lines)

    def _t_scan_universe(self, inp: dict) -> str:
        from ..analytics.features import compute_features

        sort_by = (inp.get("sort_by") or "momentum").lower()
        days = max(self.ctx.config.settings.agents.bars_lookback_days, 40)
        rows = []
        for sym in self.ctx.config.settings.universe.core:
            try:
                df = self.ctx.broker.get_bars(sym, days=days)
            except Exception:
                continue
            if df is None or len(df) < 30:
                continue
            bars = _bars_shim(df)
            feats = compute_features(bars)
            if feats is None:
                continue
            closes = [b.close for b in bars]
            opens = [b.open for b in bars]
            vols = [b.volume for b in bars]
            last = closes[-1]
            prev = closes[-2] if len(closes) > 1 else last
            day_pct = (last - prev) / prev if prev else 0.0
            gap_pct = (opens[-1] - prev) / prev if prev else 0.0
            avg_vol = (sum(vols[-21:-1]) / 20) if len(vols) > 21 else (sum(vols[:-1]) / max(1, len(vols) - 1))
            rvol = (vols[-1] / avg_vol) if avg_vol > 0 else 0.0
            sma20 = sum(closes[-20:]) / 20 if len(closes) >= 20 else last
            sma50 = sum(closes[-50:]) / 50 if len(closes) >= 50 else sma20
            dist20 = (last - sma20) / sma20 if sma20 else 0.0
            dist50 = (last - sma50) / sma50 if sma50 else 0.0
            rows.append({
                "symbol": sym,
                "last": last,
                "day_pct": day_pct,
                "gap_pct": gap_pct,
                "rvol": rvol,
                "atr_pct": feats.atr_pct,
                "momentum": feats.momentum_20,
                "sma_gap": feats.sma_gap,
                "dist20": dist20,
                "dist50": dist50,
            })
        if not rows:
            return "no universe data available"
        key = sort_by if sort_by in rows[0] else "momentum"
        rows.sort(key=lambda r: r[key], reverse=True)
        lines = [
            f"universe scan ({len(rows)} syms) sorted by {key}",
            "sym    last   day%   gap%   rvol  atr%   mom%  smaGap  vs20   vs50",
        ]
        for r in rows:
            lines.append(
                f"{r['symbol']:<6}{r['last']:>6.1f} "
                f"{r['day_pct']*100:>+5.1f} {r['gap_pct']*100:>+5.1f} "
                f"{r['rvol']:>5.1f} {r['atr_pct']*100:>5.1f} "
                f"{r['momentum']*100:>+5.1f} {r['sma_gap']*100:>+6.1f} "
                f"{r['dist20']*100:>+5.1f} {r['dist50']*100:>+5.1f}"
            )
        return "\n".join(lines)

    def _t_get_fundamentals(self, inp: dict) -> str:
        from ..data.fundamentals import get_fundamentals

        feats = get_fundamentals(self.ctx.config, inp["symbol"])
        return feats.summary()

    def _t_get_open_orders(self, _inp: dict) -> str:
        list_fn = getattr(self.ctx.broker, "list_open_orders", None)
        if list_fn is None:
            return "broker does not support listing open orders"
        try:
            orders = list_fn() or []
        except Exception as e:
            return f"error listing open orders: {e}"
        if not orders:
            return "no open/pending orders"
        lines = ["open orders:"]
        for o in orders[:50]:
            sym = getattr(o, "symbol", "?")
            side = getattr(o, "side", "?")
            if hasattr(side, "value"):
                side = side.value
            qty = getattr(o, "qty", None) or getattr(o, "quantity", "?")
            otype = getattr(o, "order_type", None) or getattr(o, "type", "?")
            if hasattr(otype, "value"):
                otype = otype.value
            limit = getattr(o, "limit_price", None)
            status = getattr(o, "status", "open")
            if hasattr(status, "value"):
                status = status.value
            oid = getattr(o, "id", "")
            lim_s = f" @{limit}" if limit is not None else ""
            lines.append(f"- {side} {qty} {sym} {otype}{lim_s} [{status}] id={oid}")
        return "\n".join(lines)

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

        # Pre-validate sizing so the agent can self-correct in-cycle instead of
        # dying later at the guardrail (which rejects — it does not clamp).
        size_err = self._sizing_precheck(proposal)
        if size_err:
            return size_err

        self.ctx.drafts.append(proposal)
        return (f"draft #{len(self.ctx.drafts)} registered for risk review: "
                f"{proposal.side} {proposal.qty:g} {proposal.symbol} "
                f"({proposal.strategy_tag}). It will NOT execute unless the risk agent "
                f"and guardrails approve.")

    def _t_propose_vertical(self, inp: dict) -> str:
        from ..analytics.options import build_vertical, chain_rows

        max_props = self.ctx.config.settings.agents.max_proposals_per_cycle
        if len(self.ctx.drafts) >= max_props:
            return f"error: proposal budget for this cycle ({max_props}) already used"

        symbol = inp["symbol"].upper()
        direction = str(inp.get("direction", "")).lower()
        lim = self.ctx.config.limits
        settings = self.ctx.config.settings.agents

        chain = self.ctx.broker.get_options_chain(symbol)
        if not chain:
            return f"no options chain for {symbol}"
        spot = self.ctx.broker.get_quote(symbol).mid
        rows = chain_rows(chain, symbol, spot, max_dte=settings.options_chain_max_dte,
                          min_dte=lim.options.min_days_to_expiry, moneyness_band=0.15)
        if not rows:
            return f"no near-the-money contracts within {settings.options_chain_max_dte} DTE for {symbol}"

        # Never let the requested budget exceed the hard options cap; the guardrail
        # would reject it anyway. max_contracts halved: the cap counts summed leg qty.
        budget = min(float(inp.get("max_loss_usd") or lim.options.max_loss_per_trade_usd),
                     lim.options.max_loss_per_trade_usd)
        mode = str(inp.get("structure", "debit")).lower()
        plan, note = build_vertical(
            rows, direction=direction, spot=spot, max_loss_usd=budget, mode=mode,
            target_dte=inp.get("target_dte"),
            max_contracts=max(lim.options.max_contracts_per_order // 2, 1),
        )
        if plan is None:
            return f"error: could not build a {mode} {direction or '?'} vertical for {symbol}: {note}"

        tag = inp.get("strategy_tag") or f"{plan.mode}-{plan.right}-vertical"
        proposal = OrderProposal(
            agent=self.ctx.agent_name, strategy_tag=tag, symbol=symbol,
            asset_class="option", side="buy", qty=0, order_type="limit",
            legs=[OptionLeg(**leg) for leg in plan.legs],
            thesis=inp["thesis"], expected_edge_usd=inp["expected_edge_usd"],
            confidence=inp.get("confidence"),
        )
        size_err = self._sizing_precheck(proposal)
        if size_err:
            return size_err

        self.ctx.drafts.append(proposal)
        return (f"draft #{len(self.ctx.drafts)} registered for risk review: "
                f"{plan.describe()} [{tag}]. It will NOT execute unless the risk agent "
                f"and guardrails approve.")

    def _sizing_precheck(self, proposal: OrderProposal) -> str | None:
        """Return an actionable error if the draft will fail hard position/risk
        caps; None if size looks within bounds. Skipped for position-reducing
        orders (exits are always allowed through sizing)."""
        if proposal.reduces_position:
            return None
        lim = self.ctx.config.limits
        equity = self.ctx.account_state.equity
        if equity <= 0:
            return None

        if proposal.asset_class == "stock":
            return self._stock_sizing_error(proposal, lim, equity)
        return self._option_sizing_error(proposal, lim)

    def _stock_sizing_error(self, proposal: OrderProposal, lim, equity: float) -> str | None:
        import math

        from ..guardrails.account_math import size_stock_position

        price = float(proposal.limit_price or 0)
        if price <= 0:
            return None
        notional = price * proposal.qty
        pos_cap = min(lim.position.max_position_usd,
                      equity * lim.position.max_position_pct / 100.0)
        order_cap = lim.orders.max_order_notional_usd
        hard_cap = min(pos_cap, order_cap)

        max_by_notional = math.floor(hard_cap / price) if price > 0 else 0
        max_by_risk = size_stock_position(
            equity=equity,
            risk_per_trade_pct=lim.position.risk_per_trade_pct,
            entry_price=price,
            stop_price=float(proposal.stop_price or 0),
            max_position_usd=lim.position.max_position_usd,
            max_position_pct=lim.position.max_position_pct,
        )
        max_qty = min(max_by_notional, max_by_risk) if max_by_risk > 0 else max_by_notional

        if notional > hard_cap:
            return (f"error: notional ${notional:,.2f} exceeds cap ${hard_cap:,.2f} "
                    f"(min of ${lim.position.max_position_usd:,.0f} position / "
                    f"{lim.position.max_position_pct:g}% equity / "
                    f"${lim.orders.max_order_notional_usd:,.0f} order) — "
                    f"max qty at this limit price is {max_qty}. Re-propose with qty={max_qty}.")

        if proposal.stop_price and proposal.qty > 0:
            per_share_risk = abs(price - float(proposal.stop_price))
            risk_usd = per_share_risk * proposal.qty
            risk_cap = equity * lim.position.risk_per_trade_pct / 100.0
            if risk_usd > risk_cap + 1e-6:
                return (f"error: risk ${risk_usd:,.2f} ({per_share_risk:.2f}/share x "
                        f"{proposal.qty:g}) exceeds {lim.position.risk_per_trade_pct:g}% "
                        f"of equity (${risk_cap:,.2f}) — max qty at this stop is "
                        f"{max_by_risk}. Re-propose with qty={max_by_risk}.")
        return None

    def _option_sizing_error(self, proposal: OrderProposal, lim) -> str | None:
        from ..guardrails.account_math import analyze_option_legs

        if not proposal.legs:
            return None
        account = self.ctx.account_state
        existing = account.position_for(proposal.symbol)
        shares = existing.qty if existing and existing.asset_class == "stock" else 0.0
        analysis = analyze_option_legs(
            proposal.legs,
            underlying_shares_held=shares,
            cash_available=account.cash,
        )
        if analysis.max_loss_usd > lim.options.max_loss_per_trade_usd:
            return (f"error: computed max loss ${analysis.max_loss_usd:,.2f} exceeds "
                    f"options cap ${lim.options.max_loss_per_trade_usd:,.0f} — "
                    f"reduce contracts or tighten the structure and re-propose.")
        return None

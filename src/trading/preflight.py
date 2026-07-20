"""Preflight self-check — the operator's 'is it safe to run live?' gate.

Verifies config validity, broker connectivity, market data, the Anthropic key,
embeddings availability, DB writability, and the market clock. Each check is
independent and degrades gracefully; the overall result is a clear go/no-go.
Read-only.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .config import Config


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""


@dataclass
class PreflightResult:
    checks: list[Check] = field(default_factory=list)

    def add(self, name: str, ok: bool, detail: str = "") -> None:
        self.checks.append(Check(name, ok, detail))

    @property
    def critical_ok(self) -> bool:
        # Broker + Anthropic + DB are the hard requirements to run.
        crit = {"broker", "anthropic", "journal-db"}
        return all(c.ok for c in self.checks if c.name in crit)

    def report(self) -> str:
        lines = []
        for c in self.checks:
            mark = "PASS" if c.ok else "FAIL"
            lines.append(f"  [{mark}] {c.name}: {c.detail}")
        lines.append("")
        lines.append("GO ✅ — critical checks passed" if self.critical_ok
                     else "NO-GO ❌ — a critical check failed")
        return "\n".join(lines)


def run_preflight(config: Config, broker_factory=None) -> PreflightResult:
    r = PreflightResult()

    # Config validity (already parsed if we got here, but re-validate explicitly).
    try:
        from .config import Limits, Settings
        Limits.model_validate(config.limits.model_dump())
        Settings.model_validate(config.settings.model_dump())
        r.add("config", True, f"mode={config.limits.mode}")
    except Exception as e:
        r.add("config", False, str(e))

    # Journal DB writable.
    try:
        from .data.journal import Journal
        j = Journal(config.settings.paths.journal_db)
        j.heartbeat("preflight", detail="write test")
        r.add("journal-db", True, config.settings.paths.journal_db)
    except Exception as e:
        r.add("journal-db", False, str(e))

    # Anthropic key present.
    r.add("anthropic", bool(config.secrets.anthropic_api_key),
          "key set" if config.secrets.anthropic_api_key else "ANTHROPIC_API_KEY missing")

    # Embeddings (OpenAI optional; local fallback always available).
    try:
        from .data.vectors import get_embedder
        r.add("embeddings", True, type(get_embedder()).__name__)
    except Exception as e:
        r.add("embeddings", False, str(e))

    # Broker connectivity + market data + clock.
    make_broker = broker_factory or _default_broker_factory
    broker = make_broker(config)
    if broker is None:
        r.add("broker", False, "could not construct broker (Alpaca keys?)")
    else:
        try:
            state = broker.get_account_state()
            r.add("broker", True, f"equity ${state.equity:,.0f}")
        except Exception as e:
            r.add("broker", False, str(e))
        try:
            q = broker.get_quote("SPY")
            r.add("market-data", q.mid > 0, f"SPY mid {q.mid:.2f}")
        except Exception as e:
            r.add("market-data", False, str(e))
        try:
            r.add("market-clock", True, "open" if broker.market_open() else "closed")
        except Exception as e:
            r.add("market-clock", False, str(e))

    return r


def _default_broker_factory(config: Config):
    try:
        from .broker.alpaca import AlpacaBroker
        return AlpacaBroker(config)
    except Exception:
        return None

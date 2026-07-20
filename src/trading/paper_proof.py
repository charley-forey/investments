"""Paper-proof readiness check — M18 lite go/no-go for unattended paper.

Verifies the learning + execution path is wired so the first fill can complete
fill → lot → score → lesson, and that ops hooks (Discord, calendar, daemon
prereqs) are in place. Extends preflight; does not place orders.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from .config import Config
from .preflight import Check, PreflightResult, run_preflight


@dataclass
class PaperProofResult:
    preflight: PreflightResult
    checks: list[Check] = field(default_factory=list)

    def add(self, name: str, ok: bool, detail: str = "") -> None:
        self.checks.append(Check(name, ok, detail))

    @property
    def ok(self) -> bool:
        # Paper-proof soft-fails Discord; hard-requires preflight critical + mode paper
        # + calendar + sizing path.
        hard = {"mode-paper", "calendar", "sizing-precheck", "journal-schema"}
        return self.preflight.critical_ok and all(
            c.ok for c in self.checks if c.name in hard
        )

    def report(self) -> str:
        lines = ["# Paper-proof readiness", "", "## Preflight"]
        lines.append(self.preflight.report())
        lines.append("")
        lines.append("## Paper-proof checks")
        for c in self.checks:
            mark = "PASS" if c.ok else "FAIL"
            lines.append(f"  [{mark}] {c.name}: {c.detail}")
        lines.append("")
        if self.ok:
            lines.append(
                "GO — run `trading daemon` (and optionally `trading stream`). "
                "Confirm Discord on first fill; after close, check scores + lessons."
            )
        else:
            lines.append("NO-GO — fix FAIL items above before unattended paper.")
        return "\n".join(lines)


def _check_journal_schema(config: Config, result: PaperProofResult) -> None:
    from .data.journal import Journal

    j = Journal(config.settings.paths.journal_db)
    try:
        lot_cols = {r["name"] for r in j.conn.execute("PRAGMA table_info(tax_lots)")}
        tables = {r[0] for r in j.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        has_pid = "proposal_id" in lot_cols
        has_outcomes = "proposal_outcomes" in tables
        ok = has_pid and has_outcomes
        result.add(
            "journal-schema", ok,
            f"tax_lots.proposal_id={'yes' if has_pid else 'NO'}; "
            f"proposal_outcomes={'yes' if has_outcomes else 'NO'}",
        )
        # Path status (informational)
        n_prop = j.conn.execute("SELECT COUNT(*) AS n FROM proposals").fetchone()["n"]
        n_fills = j.conn.execute("SELECT COUNT(*) AS n FROM fills").fetchone()["n"]
        n_scores = j.conn.execute("SELECT COUNT(*) AS n FROM scores").fetchone()["n"]
        n_out = j.conn.execute(
            "SELECT COUNT(*) AS n FROM proposal_outcomes").fetchone()["n"]
        result.add(
            "track-record", n_fills > 0,
            f"proposals={n_prop} fills={n_fills} scores={n_scores} "
            f"counterfactuals={n_out}"
            + ("" if n_fills else " — waiting on first paper fill"),
        )
    finally:
        j.close()


def _check_calendar(config: Config, result: PaperProofResult) -> None:
    path = Path(config.settings.paths.calendar_file)
    if not path.exists():
        result.add("calendar", False, f"missing {path}")
        return
    try:
        events = json.loads(path.read_text(encoding="utf-8"))
        n = len(events) if isinstance(events, list) else 0
        result.add("calendar", n > 0, f"{n} events in {path}")
    except (ValueError, OSError) as e:
        result.add("calendar", False, str(e))


def _check_discord(config: Config, result: PaperProofResult) -> None:
    url = getattr(config.secrets, "discord_webhook_url", "") or ""
    result.add(
        "discord", bool(url),
        "DISCORD_WEBHOOK_URL set" if url else
        "optional but recommended — fills/errors won't ping without it",
    )


def _check_sizing_precheck(config: Config, result: PaperProofResult) -> None:
    """Confirm oversized drafts are rejected with an actionable max qty."""
    from .broker.models import AccountState
    from .data.journal import Journal
    from .tools.registry import STRATEGY_TOOLS, ToolContext, ToolRegistry

    class _B:
        def get_quote(self, symbol):
            from .broker.models import Quote
            return Quote(symbol=symbol, bid=99.9, ask=100.1)

    j = Journal(config.settings.paths.journal_db)
    try:
        account = AccountState(
            mode="paper", equity=100_000, cash=100_000, buying_power=200_000,
            last_equity=100_000, daytrade_count=0, pattern_day_trader=False,
        )
        ctx = ToolContext(config=config, journal=j, broker=_B(),
                          account_state=account, agent_name="strategy")
        reg = ToolRegistry(ctx, STRATEGY_TOOLS)
        out = reg.dispatch("propose_order", {
            "symbol": "META", "asset_class": "stock", "side": "buy", "qty": 40,
            "limit_price": 650.0, "stop_price": 630.0, "thesis": "size check",
            "expected_edge_usd": 200.0, "strategy_tag": "paper-proof",
        })
        ok = out.startswith("error:") and "max qty" in out
        result.add("sizing-precheck", ok,
                   "rejects oversized with max qty" if ok else out[:120])
    except Exception as e:
        result.add("sizing-precheck", False, str(e))
    finally:
        j.close()


def _check_mode(config: Config, result: PaperProofResult) -> None:
    paper = not config.is_live
    result.add("mode-paper", paper,
               f"mode={config.limits.mode}" + ("" if paper else " — switch to paper for M18"))


def _check_playbooks(config: Config, result: PaperProofResult) -> None:
    d = Path(config.settings.paths.playbooks_dir)
    files = list(d.glob("*.md")) if d.exists() else []
    result.add("playbooks", bool(files),
               f"{len(files)} playbook(s) in {d}" if files else f"no playbooks in {d}")


def run_paper_proof(config: Config, broker_factory=None) -> PaperProofResult:
    pf = run_preflight(config, broker_factory=broker_factory)
    result = PaperProofResult(preflight=pf)
    _check_mode(config, result)
    _check_journal_schema(config, result)
    _check_calendar(config, result)
    _check_discord(config, result)
    _check_sizing_precheck(config, result)
    _check_playbooks(config, result)
    return result

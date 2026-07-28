"""Operator CLI: inspect the account, push manual orders through the guardrails,
approve pending live orders, and check system status."""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, time, timedelta, timezone

from .config import get_config
from .data.journal import Journal
from .guardrails.account_math import account_snapshot_summary
from .guardrails.engine import OrderPipeline
from .guardrails.models import OrderProposal


def _journal() -> Journal:
    return Journal(get_config().settings.paths.journal_db)


def _broker():
    from .broker.alpaca import AlpacaBroker

    return AlpacaBroker(get_config())


def cmd_account(_args) -> int:
    broker = _broker()
    state = broker.get_account_state(_journal())
    print(account_snapshot_summary(state))
    return 0


def cmd_quote(args) -> int:
    q = _broker().get_quote(args.symbol)
    print(f"{q.symbol}: bid {q.bid:.2f} x {q.bid_size:g}  ask {q.ask:.2f} x {q.ask_size:g}  "
          f"mid {q.mid:.2f}  spread {q.spread:.2f}")
    return 0


def cmd_propose(args) -> int:
    config = get_config()
    journal = _journal()
    broker = _broker()
    pipeline = OrderPipeline(config, journal, broker)

    proposal = OrderProposal(
        agent="human",
        symbol=args.symbol,
        side=args.side,
        qty=args.qty,
        order_type="limit",
        limit_price=args.limit,
        stop_price=args.stop,
        expected_edge_usd=args.edge,
        thesis=args.thesis,
        reduces_position=args.reduces,
    )
    result = pipeline.process(
        proposal,
        broker.get_account_state(journal),
        broker.get_quote(args.symbol),
        market_is_open=broker.market_open(),
    )
    print(f"proposal #{result.proposal_id}: {result.status}")
    if result.result.notional_usd is not None:
        print(f"  notional ${result.result.notional_usd:,.2f}  "
              f"est cost ${result.result.est_cost_usd:,.2f}")
    for v in result.result.violations:
        print(f"  REJECTED [{v.rule}] {v.message}")
    if result.broker_order_id:
        print(f"  broker order id: {result.broker_order_id}")
    return 0 if result.status != "rejected" else 1


def cmd_pending(_args) -> int:
    rows = _journal().pending_approvals()
    if not rows:
        print("no orders pending approval")
        return 0
    for r in rows:
        print(f"#{r['id']} {r['side']} {r['qty']:g} {r['symbol']} "
              f"@ {r['limit_price']} ({r['strategy_tag']}) — {r['thesis'] or 'no thesis'}")
    return 0


def cmd_approve(args) -> int:
    config = get_config()
    journal = _journal()
    pipeline = OrderPipeline(config, journal, _broker())
    result = pipeline.approve(args.proposal_id)
    print(f"proposal #{args.proposal_id}: {result.status}")
    if result.broker_order_id:
        print(f"  broker order id: {result.broker_order_id}")
    return 0


def cmd_status(_args) -> int:
    config = get_config()
    journal = _journal()
    now = datetime.now(timezone.utc)
    day_start = datetime.combine(now.date(), time.min, tzinfo=timezone.utc)
    week_start = day_start - timedelta(days=now.weekday())

    ks = "ACTIVE" if journal.kill_switch_active() else "off"
    print(f"mode: {config.limits.mode}")
    print(f"kill switch: {ks}")
    if journal.kill_switch_active():
        print(f"  reason: {journal.get_state('kill_switch_reason')}")
        print(f"  since:  {journal.get_state('kill_switch_ts')}")
    halt = (journal.get_state("reconcile_halt") or "").strip()
    if halt:
        print(f"reconcile halt: {halt}")
    print(f"trades today: {journal.trades_since(day_start)}/{config.limits.orders.max_new_trades_per_day}")
    print(f"trades this week: {journal.trades_since(week_start)}/{config.limits.orders.max_new_trades_per_week}")
    print(f"day trades (5d, journal): {journal.day_trades_last_n_days(5)}")
    print(f"pending approvals: {len(journal.pending_approvals())}")

    # Liveness + cost.
    from .monitoring import check_health

    health = check_health(journal)
    print(f"health: {health.summary()}")
    last = journal.last_successful_cycle()
    if last:
        print(f"last successful cycle: {last['ts']} ({last['detail']})")
    day_ago = (now - timedelta(days=1)).isoformat()
    cost_24h = journal.cost_since(day_ago)
    submitted_24h = journal.conn.execute(
        "SELECT COUNT(*) AS n FROM proposals WHERE status='submitted' AND ts >= ?",
        (day_ago,),
    ).fetchone()["n"]
    print(f"Anthropic cost (24h): ${cost_24h:.2f}")
    print(f"submitted trades (24h): {submitted_24h}")
    if submitted_24h:
        print(f"cost per submitted trade: ${cost_24h / submitted_24h:.2f}")
    try:
        equity = float(journal.conn.execute(
            "SELECT equity FROM equity_snapshots ORDER BY id DESC LIMIT 1"
        ).fetchone()["equity"])
        if equity > 0:
            print(f"cost as % of equity (24h): {100.0 * cost_24h / equity:.3f}%")
    except Exception:
        pass
    return 0


def cmd_metrics(_args) -> int:
    import json

    from .monitoring import metrics_snapshot

    print(json.dumps(metrics_snapshot(get_config(), _journal()), indent=2))
    return 0


def cmd_tokens(args) -> int:
    """Where the Anthropic bill actually goes, per day / agent / model.

    `status` gives a 24h total, which is enough to enforce the cap and useless for
    deciding what to change. Every cost decision so far turned on two things this
    prints and that one does not: the input-vs-output split (it inverted when we
    moved off Sonnet, and inverting it again changes which lever is worth pulling)
    and whether an agent is billing without being metered at all.
    """
    from . import cost as cost_mod

    days = int(getattr(args, "days", 7) or 7)
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    journal = _journal()
    rows = journal.conn.execute(
        """SELECT substr(ts,1,10) AS d, agent, model, COUNT(*) AS n,
                  SUM(input_tokens) AS ti, SUM(cache_read_tokens) AS tr,
                  SUM(cache_write_tokens) AS tw, SUM(output_tokens) AS to_,
                  SUM(cost_usd) AS c
             FROM usage WHERE ts >= ?
            GROUP BY d, agent, model ORDER BY d, c DESC""",
        (since,),
    ).fetchall()
    if not rows:
        print(f"no metered LLM usage in the last {days}d.")
        return 0

    print(f"{'date':<11} {'agent':<9} {'model':<19} {'n':>3} "
          f"{'in':>7} {'cached':>8} {'write':>7} {'out':>7} {'$':>7} {'$/sess':>7}")
    in_cost = out_cost = 0.0
    for r in rows:
        print(f"{r['d']:<11} {r['agent']:<9} {r['model']:<19} {r['n']:>3} "
              f"{r['ti'] // r['n']:>7} {r['tr'] // r['n']:>8} {(r['tw'] or 0) // r['n']:>7} "
              f"{r['to_'] // r['n']:>7} {r['c']:>7.2f} {r['c'] / r['n']:>7.3f}")
        # Split spend into input vs output so the next optimisation targets the
        # bigger half. Same formula the cap is enforced with, not a copy of it.
        i, o = cost_mod.split_cost(
            cost_mod.Usage(r["ti"], r["to_"], r["tr"], r["tw"] or 0), r["model"])
        in_cost += i
        out_cost += o

    total = in_cost + out_cost
    stored = sum(r["c"] for r in rows)
    sessions = sum(r["n"] for r in rows)
    print(f"\ntotal ${total:.2f} over {days}d  (${total / days:.2f}/day, "
          f"{sessions} sessions, ${total / sessions:.3f}/session)")
    if total > 0:
        print(f"input  ${in_cost:.2f}  ({100 * in_cost / total:.0f}%)   "
              f"output ${out_cost:.2f}  ({100 * out_cost / total:.0f}%)"
              f"   <- optimise the bigger half")
    # The $ column is what was stored at record time; the total is recomputed from
    # tokens at today's rates. They diverge when rows predate a pricing fix, and
    # the cap is enforced on the stored number -- so a gap here means the cap is
    # loose by exactly that much.
    if stored > 0 and abs(total - stored) / stored > 0.05:
        print(f"NOTE: rows store ${stored:.2f} but today's rates on the same tokens "
              f"give ${total:.2f} ({100 * (total - stored) / stored:+.0f}%). Older rows "
              f"were costed before input/cache-read were treated as disjoint; the "
              f"daily cap is enforced on the understated figure.")

    # Full history is re-sent every tool iteration, so cached reads are a multiple
    # of the unique context. That multiple is what makes trimming a tool result
    # worth more than its face size -- and it is invisible in the raw totals.
    reads = sum(r["tr"] for r in rows)
    if reads and sessions:
        iters = get_config().settings.agents.max_tool_iterations_intraday or 12
        unique = (reads / sessions) / (iters * (iters + 1) / 2) * iters
        if unique > 0:
            print(f"cache re-read amplification: {(reads / sessions) / unique:.1f}x "
                  f"(~{unique:,.0f} unique tokens/session re-sent across {iters} iterations)")

    missing = {"strategy", "risk", "redteam", "scoring", "intel"} - {r["agent"] for r in rows}
    if missing:
        print(f"\nWARNING: no usage rows for {', '.join(sorted(missing))}. "
              f"If those agents ran, they were billed but not metered -- the daily "
              f"cap is policing a fraction of real spend. Usually the daemon is on "
              f"an older checkout than the merged orchestrator.")
    return 0


def cmd_preflight(_args) -> int:
    from .preflight import run_preflight

    result = run_preflight(get_config())
    print(result.report())
    return 0 if result.critical_ok else 1


def cmd_paper_proof(_args) -> int:
    """M18 lite: go/no-go for unattended paper + learning-loop wiring."""
    from .paper_proof import run_paper_proof

    result = run_paper_proof(get_config())
    print(result.report())
    return 0 if result.ok else 1


def cmd_calendar(_args) -> int:
    from .data.calendar_feed import refresh_calendar

    report = refresh_calendar(get_config())
    print(f"calendar → {report.path}: {report.events_written} events "
          f"({report.symbols_ok} symbols ok, {report.symbols_failed} failed)")
    for err in report.errors[:5]:
        print(f"  warn: {err}")
    return 0 if report.events_written > 0 or report.symbols_ok > 0 else 1


def cmd_notify_test(_args) -> int:
    """Send a one-shot Discord ping to verify DISCORD_WEBHOOK_URL is wired."""
    from . import notify

    config = get_config()
    if not config.secrets.discord_webhook_url:
        print("DISCORD_WEBHOOK_URL is empty in .env — paste a webhook URL first.")
        print("See .env.example for setup steps, then re-run: trading notify-test")
        return 1
    ok = notify.send(config, "✅ Trading notify-test: Discord webhook is working.")
    if ok:
        print("sent — check your Discord channel")
        return 0
    print("failed to post (check the webhook URL / network)")
    return 1


def cmd_watchdog(_args) -> int:
    from .monitoring import run_watchdog

    health = run_watchdog(get_config(), _journal())
    print(health.summary())
    return 0 if health.healthy else 1


def cmd_backup(_args) -> int:
    from .backup import backup_journal

    dest = backup_journal(get_config())
    print(f"journal backed up to {dest}")
    return 0


def cmd_stream(_args) -> int:
    import logging

    from .broker.stream import run_trade_stream

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    run_trade_stream(get_config())
    return 0


def cmd_marketstream(_args) -> int:
    import logging

    from .broker.market_stream import run_market_stream

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    run_market_stream(get_config())
    return 0


def cmd_run_once(args) -> int:
    from .agents.client import make_client
    from .orchestrator import Orchestrator

    config = get_config()
    journal = _journal()
    broker = _broker()
    client = make_client(config)
    orch = Orchestrator(config, journal, broker, client)
    report = orch.run_cycle(args.cycle)
    print(report.summary())
    for note in report.notes:
        print(f"  note: {note}")
    return 0


def cmd_movers(_args) -> int:
    """Run one deterministic movers / OpportunityScore scan."""
    from .scanner.movers import load_candidates, run_movers_scan

    config = get_config()
    journal = _journal()
    report = run_movers_scan(config, _broker(), journal=journal)
    print(f"screened={report.screened} candidates={report.candidates} -> {report.path}")
    for c in load_candidates(config):
        print(
            f"  {c['symbol']:<6} score={c.get('score', 0):5.1f} "
            f"day={100*float(c.get('day_pct') or 0):+5.1f}% "
            f"rvol={float(c.get('rvol') or 0):4.1f}x "
            f"tmpl={c.get('template')} src={c.get('discovery_source')}"
        )
    return 0


def cmd_stats(_args) -> int:
    from .analytics.lifecycle import stages_summary
    from .analytics.stats import portfolio_summary
    from .scanner.learning import stats_by_source

    config = get_config()
    journal = _journal()
    print(portfolio_summary(journal, config.settings.tax))
    print("\nStrategy stages: " + stages_summary(journal))
    print()
    print(stats_by_source(journal))
    return 0


def _load_bars_for(symbol, days):
    """Prefer the persisted bar store; fall back to a live broker fetch."""
    from backtest.engine import Bar
    from trading.data.bars import BarStore

    config = get_config()
    store = BarStore(config.settings.paths.bars_db)
    rows = store.load_bars(symbol)
    if rows:
        return [Bar(date=b.date, open=b.open, high=b.high, low=b.low,
                    close=b.close, volume=b.volume) for b in rows]
    from backtest.engine import bars_from_alpaca_df

    df = _broker().get_bars(symbol, days=days)
    return bars_from_alpaca_df(df) if df is not None else []


def cmd_ingest(args) -> int:
    from trading.data.bars import BarStore, ingest_symbol

    config = get_config()
    store = BarStore(config.settings.paths.bars_db)
    broker = _broker()
    symbols = args.symbols or config.settings.universe.core
    total = 0
    for sym in symbols:
        n = ingest_symbol(store, broker, sym, days=args.days)
        total += n
        print(f"  {sym}: {n} bars")
    print(f"ingested {total} bars for {len(symbols)} symbol(s)")
    return 0


SIGNALS = {
    "sma": "sma_crossover",
    "breakout": "breakout",
    "trend-pullback-long": "trend_pullback_long",
    "momentum-continuation": "momentum_continuation",
}


def cmd_backtest(args) -> int:
    import backtest.strategies as strat
    from backtest.engine import run_backtest

    bars = _load_bars_for(args.symbol, args.days)
    if not bars:
        print(f"no bars for {args.symbol}")
        return 1
    signal = getattr(strat, SIGNALS[args.strategy])()
    bt_kw = dict(stop_pct=args.stop_pct, target_r=args.target_r,
                 allow_shorts=args.shorts)

    if args.walkforward:
        from backtest.walkforward import gate_strategy, walk_forward

        wf = walk_forward(bars, signal)
        print(f"{args.symbol} {args.strategy} walk-forward: {wf.summary()} "
              f"-> {'PASS' if wf.passed() else 'FAIL'}")
        if args.tag:
            print("  " + gate_strategy(_journal(), args.tag, wf))
        return 0

    result = run_backtest(bars, signal, **bt_kw)
    print(f"{args.symbol} {args.strategy} over {len(bars)} bars: {result.summary()}")
    if args.stop_pct:
        print(f"  exits: {result.exit_breakdown()}")

    if args.montecarlo:
        from backtest.montecarlo import bootstrap

        mc = bootstrap([t.net_pnl for t in result.trades])
        print("  " + mc.summary())

    if args.benchmark:
        from backtest.metrics import compute_metrics

        bench_bars = _load_bars_for(args.benchmark, args.days)
        bench_curve = run_backtest(bench_bars, signal).equity_curve if bench_bars else None
        m = compute_metrics(result.equity_curve, bench_curve)
        print(f"  metrics: {m.summary()}")

    if args.promote and args.tag:
        from .analytics.lifecycle import promote_after_backtest

        change = promote_after_backtest(_journal(), args.tag, result.expectancy)
        print(f"  {'promoted ' + change.tag + ': ' + change.old_stage + ' -> ' + change.new_stage if change else 'no promotion for ' + str(args.tag)}")
    return 0


def cmd_tax(args) -> int:
    from .analytics.tax import (
        apply_wash_sale_adjustments, export_realized_gains_csv, harvest_candidates,
        realized_gains_report, realized_totals,
    )

    config = get_config()
    journal = _journal()

    if args.action == "wash":
        adj = apply_wash_sale_adjustments(journal, config.limits.wash_sale.window_days)
        print(f"applied {len(adj)} wash-sale adjustment(s)")
        for a in adj:
            print(f"  loss lot#{a.loss_lot_id} {a.symbol}: ${a.disallowed_usd:,.2f} "
                  f"deferred into lot#{a.replacement_lot_id}")
        return 0

    if args.action == "report":
        rows = realized_gains_report(journal, args.year)
        t = realized_totals(rows)
        for r in rows:
            print(f"{r.close_ts[:10]} {r.symbol:<12} {r.term:<5} "
                  f"realized ${r.allowed_pnl:+,.2f}"
                  + (f" (wash ${r.wash_disallowed:,.2f} deferred)" if r.wash_disallowed else ""))
        print(f"\nShort-term ${t['short_term']:+,.2f}  Long-term ${t['long_term']:+,.2f}  "
              f"Total ${t['total']:+,.2f}  Wash-deferred ${t['wash_disallowed']:,.2f}  "
              f"({t['trades']} trades)")
        return 0

    if args.action == "export":
        path = args.path or f"data/realized_gains_{args.year or 'all'}.csv"
        n = export_realized_gains_csv(journal, path, args.year)
        print(f"exported {n} rows to {path}")
        return 0

    if args.action == "harvest":
        cands = harvest_candidates(_broker().get_account_state(journal), args.min_loss)
        if not cands:
            print("no tax-loss-harvest candidates")
        for c in cands:
            print("  " + c.summary())
        return 0
    return 1


def cmd_edge(_args) -> int:
    from .analytics.edge import benchmark_comparison, portfolio_edge, strategy_edges

    config = get_config()
    journal = _journal()
    print("Proof of alpha — per-strategy realized edge (paper track record):")
    for e in strategy_edges(journal):
        print("  " + e.summary())
    pe = portfolio_edge(journal)
    print(f"\n{pe['verdict']} ({pe['total_scored_trades']} scored trades)")
    bc = benchmark_comparison(journal, config.settings.paths.bars_db)
    if bc.get("available"):
        line = f"Account return {bc['account_return']*100:+.2f}%"
        if bc.get("benchmark_return") is not None:
            line += (f" vs {bc['benchmark']} {bc['benchmark_return']*100:+.2f}%"
                     f" (excess {bc['excess_return']*100:+.2f}%)")
        print(line)
    return 0


def cmd_allocate(_args) -> int:
    from .analytics.allocation import allocate_capital, attribution_report

    config = get_config()
    journal = _journal()
    print("Capital allocation (by risk-adjusted after-tax expectancy):")
    for a in allocate_capital(journal, config.settings.tax):
        print(f"  {a.tag:<16} weight {a.weight*100:5.1f}%  "
              f"after-tax expectancy ${a.after_tax_expectancy:+.2f}  "
              f"({a.trades} trades, conf {a.confidence})")
    print("\nP&L attribution:")
    for r in attribution_report(journal, config.settings.tax):
        print(f"  {r.tag:<16} after-tax ${r.after_tax_pnl:+.2f}  "
              f"({r.share*100:.0f}% of total, {r.trades} trades)")
    return 0


def cmd_scale(args) -> int:
    from .analytics import scaling

    config = get_config()
    journal = _journal()
    if args.action == "status":
        print(scaling.status(journal))
        for lvl in (1, 2, 3):
            e = scaling.check_eligibility(journal, config.settings.tax, lvl)
            print(f"  level {lvl} (x{scaling.LADDER[lvl]}): "
                  f"{'ELIGIBLE' if e.eligible else 'not yet'} — {e.reason}")
        return 0
    if args.action == "approve":
        e = scaling.approve_level(journal, config.settings.tax, args.level)
        if e.eligible:
            print(f"live-scaling level set to {args.level}. {scaling.status(journal)}")
            return 0
        print(f"refused: not eligible for level {args.level} — {e.reason}")
        return 1
    return 1


def cmd_risk(args) -> int:
    from .analytics import risk_profile as rp
    from .config import load_config

    config = get_config()
    journal = _journal()

    if not args.profile:
        active = rp.read_active(config)
        # load_config (uncached) reflects the on-disk active profile's effective caps.
        print(f"active risk profile: {active}")
        print(f"  effective: {rp.effective_summary(load_config().limits)}")
        for name in ("conservative", "balanced", "aggressive"):
            d = rp.check_eligibility(journal, config.settings.tax, name)
            mark = "current" if name == active else (
                "available" if d.eligible else f"locked — {d.reason}")
            print(f"  {name:<12} {mark}")
        print("\nSet with:  trading risk <profile> [--force]")
        return 0

    d = rp.set_profile(config, journal, config.settings.tax, args.profile, force=args.force)
    if not d.eligible:
        print(f"refused: '{args.profile}' not available — {d.reason}")
        print("  override with --force (records an unproven-edge warning in the journal).")
        return 1
    if args.force and "forced" in d.reason:
        print(f"⚠️  WARNING: '{args.profile}' set by FORCE with an unproven edge "
              f"({rp.check_eligibility(journal, config.settings.tax, args.profile).reason}). "
              f"Override journaled.")
    print(f"risk profile set to '{args.profile}'.")
    print(f"  effective: {rp.effective_summary(load_config().limits)}")
    return 0


def cmd_execution(_args) -> int:
    from .execution import fill_quality_report

    config = get_config()
    q = fill_quality_report(_journal())
    print(q.summary())
    print(f"(current cost-hurdle slippage assumption: "
          f"{config.limits.cost_hurdle.slippage_bps} bps)")
    return 0


def cmd_decisions(_args) -> int:
    from .analytics.decision_record import list_records

    for rec in list_records(_journal(), limit=25):
        print(rec.summary_line())
    return 0


def cmd_why(args) -> int:
    from .analytics.decision_record import build_record

    rec = build_record(_journal(), args.proposal_id)
    if rec is None:
        print(f"no decision record for proposal #{args.proposal_id}")
        return 1
    print(rec.full_text())
    return 0


def cmd_intel(args) -> int:
    from .data.ingest import ingest_intel
    from .data.intel import IntelStore

    config = get_config()
    store = IntelStore(config.settings.paths.intel_db)
    if args.action == "ingest":
        report = ingest_intel(config, store, _broker())
        print(f"news +{report.news_saved}, sentiment snapshots +{report.sentiment_snapshots} "
              f"across {report.symbols} symbols")
        return 0
    if args.action == "digest":
        d = store.latest_digest()
        print(d["digest_md"] if d else "no market-intel digest yet — run a cycle or ingest")
        return 0
    if args.action == "show":
        for n in store.recent_news(args.symbol, limit=15):
            print(f"  [{n['ts'][:16]}] {n['symbol']} {n['headline']} ({n['source']})")
        for s in store.sentiment_history(args.symbol or "", days=7) if args.symbol else []:
            print(f"  sentiment {s['ts'][:16]} {s['symbol']} polarity {s['polarity']:+.2f}")
        return 0
    return 1


def cmd_sync(_args) -> int:
    from .broker.sync import sync_fills

    config = get_config()
    report = sync_fills(config, _journal(), _broker())
    print(f"orders={report.orders_seen} fills={report.fills_recorded} "
          f"lots+{report.lots_opened}/-{report.lots_closed} "
          f"day_trades={report.day_trades_flagged}")
    for w in report.reconciliation_warnings:
        print(f"  WARN reconcile: {w}")
    return 0


def cmd_dashboard(args) -> int:
    try:
        import uvicorn
    except ImportError:
        print("dashboard needs the web extra: pip install -e \".[web]\"")
        return 2
    import os

    from .web.app import _LOOPBACK, create_app

    token = os.environ.get("DASHBOARD_TOKEN") or None
    if args.host not in _LOOPBACK and not token:
        print(f"refusing to bind {args.host} without a token: these endpoints approve\n"
              "proposals and submit orders. Set DASHBOARD_TOKEN in the environment,\n"
              "or bind 127.0.0.1 for local-only access.")
        return 2
    print(f"observability dashboard on http://{args.host}:{args.port}"
          + ("  (token required)" if token else ""))
    uvicorn.run(create_app(), host=args.host, port=args.port, log_level="info")
    return 0


def cmd_daemon(_args) -> int:
    from .scheduler import run_daemon

    return run_daemon()


def cmd_reset_kill_switch(_args) -> int:
    journal = _journal()
    journal.reset_kill_switch()
    journal.set_state("kill_switch_reset_ts", journal.get_state("kill_switch_ts") or "")
    print("kill switch reset")
    return 0


def main(argv: list[str] | None = None) -> int:
    # LLM-generated text (digests, reasoning, watchlists) contains Unicode the
    # default Windows console (cp1252) can't encode. Force UTF-8 output.
    for stream in (sys.stdout, sys.stderr):
        reconfig = getattr(stream, "reconfigure", None)
        if reconfig:
            try:
                reconfig(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass

    p = argparse.ArgumentParser(prog="trading", description="Agentic trading system CLI")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("account", help="account snapshot").set_defaults(fn=cmd_account)

    q = sub.add_parser("quote", help="latest quote")
    q.add_argument("symbol")
    q.set_defaults(fn=cmd_quote)

    pr = sub.add_parser("propose", help="manual order through the guardrails")
    pr.add_argument("--symbol", required=True)
    pr.add_argument("--side", choices=["buy", "sell"], default="buy")
    pr.add_argument("--qty", type=float, required=True)
    pr.add_argument("--limit", type=float, required=True)
    pr.add_argument("--stop", type=float, default=None)
    pr.add_argument("--edge", type=float, default=None, help="expected edge in USD")
    pr.add_argument("--thesis", default=None)
    pr.add_argument("--reduces", action="store_true", help="closes/trims an existing position")
    pr.set_defaults(fn=cmd_propose)

    sub.add_parser("pending", help="orders awaiting live approval").set_defaults(fn=cmd_pending)

    ap = sub.add_parser("approve", help="approve a pending live order")
    ap.add_argument("proposal_id", type=int)
    ap.set_defaults(fn=cmd_approve)

    ro = sub.add_parser("run-once", help="run a single orchestrator cycle")
    ro.add_argument("--cycle", choices=["premarket", "intraday", "postclose", "weekend"],
                    default="intraday")
    ro.set_defaults(fn=cmd_run_once)

    sub.add_parser("stats", help="per-strategy performance and lifecycle stages").set_defaults(
        fn=cmd_stats
    )
    sub.add_parser(
        "movers", help="run one deterministic movers / OpportunityScore scan",
    ).set_defaults(fn=cmd_movers)

    bt = sub.add_parser("backtest", help="backtest a reference strategy on a symbol")
    bt.add_argument("symbol")
    bt.add_argument("--strategy", choices=sorted(SIGNALS), default="sma")
    bt.add_argument("--days", type=int, default=365)
    bt.add_argument("--stop-pct", type=float, default=None,
                    help="protective stop this far from entry, e.g. 0.02 for 2%%")
    bt.add_argument("--target-r", type=float, default=None,
                    help="take-profit at N x the stop distance; with --stop-pct this "
                         "is what tests whether limits.orders.min_reward_risk is right")
    bt.add_argument("--shorts", action="store_true",
                    help="allow the signal to go short (-1)")
    bt.add_argument("--tag", default=None, help="strategy tag to gate/promote")
    bt.add_argument("--promote", action="store_true",
                    help="promote candidate->paper if expectancy is positive")
    bt.add_argument("--walkforward", action="store_true",
                    help="run out-of-sample walk-forward validation + auto-gate")
    bt.add_argument("--montecarlo", action="store_true",
                    help="bootstrap the trade P&Ls for an expectancy confidence interval")
    bt.add_argument("--benchmark", default=None, help="benchmark symbol for alpha/beta")
    bt.set_defaults(fn=cmd_backtest)

    ing = sub.add_parser("ingest", help="fetch and persist bar history")
    ing.add_argument("symbols", nargs="*", help="symbols (default: configured universe)")
    ing.add_argument("--days", type=int, default=365)
    ing.set_defaults(fn=cmd_ingest)

    tx = sub.add_parser("tax", help="tax accounting: wash sales, realized gains, harvesting")
    tx.add_argument("action", choices=["wash", "report", "export", "harvest"])
    tx.add_argument("--year", type=int, default=None)
    tx.add_argument("--path", default=None, help="output path for export")
    tx.add_argument("--min-loss", type=float, default=100.0, dest="min_loss")
    tx.set_defaults(fn=cmd_tax)

    sub.add_parser("edge", help="proof of alpha: statistical edge + benchmark vs SPY").set_defaults(
        fn=cmd_edge
    )
    sub.add_parser("allocate", help="capital allocation + P&L attribution").set_defaults(
        fn=cmd_allocate
    )

    rk = sub.add_parser("risk", help="risk dial: conservative | balanced | aggressive")
    rk.add_argument("profile", nargs="?",
                    choices=["conservative", "balanced", "aggressive"],
                    help="omit to show the active profile and what's unlocked")
    rk.add_argument("--force", action="store_true",
                    help="apply a gated profile despite track record (journaled)")
    rk.set_defaults(fn=cmd_risk)

    sc = sub.add_parser("scale", help="live-scaling ladder (human-gated)")
    sc.add_argument("action", choices=["status", "approve"])
    sc.add_argument("--level", type=int, default=0)
    sc.set_defaults(fn=cmd_scale)

    sub.add_parser("decisions", help="list recent decision records").set_defaults(
        fn=cmd_decisions
    )
    wy = sub.add_parser("why", help="full decision record + reasoning for a proposal")
    wy.add_argument("proposal_id", type=int)
    wy.set_defaults(fn=cmd_why)

    it = sub.add_parser("intel", help="market-intelligence ingestion + digest")
    it.add_argument("action", choices=["ingest", "digest", "show"])
    it.add_argument("--symbol", default=None)
    it.set_defaults(fn=cmd_intel)

    tk = sub.add_parser("tokens", help="LLM spend breakdown: input vs output, per agent/model")
    tk.add_argument("--days", type=int, default=7, help="lookback window (default 7)")
    tk.set_defaults(fn=cmd_tokens)

    sub.add_parser("execution", help="fill-quality / slippage report").set_defaults(
        fn=cmd_execution
    )
    sub.add_parser("sync", help="sync fills and tax lots from the broker").set_defaults(fn=cmd_sync)
    sub.add_parser("daemon", help="run the scheduled trading daemon").set_defaults(fn=cmd_daemon)

    dash = sub.add_parser("dashboard", help="launch the local observability dashboard")
    dash.add_argument("--host", default="127.0.0.1")  # localhost-only by default
    dash.add_argument("--port", type=int, default=8787)
    dash.set_defaults(fn=cmd_dashboard)
    sub.add_parser("stream", help="run the real-time fill websocket").set_defaults(fn=cmd_stream)
    sub.add_parser("marketstream",
                   help="run the real-time market-data websocket (queues wake events)"
                   ).set_defaults(fn=cmd_marketstream)
    sub.add_parser("preflight", help="go/no-go self-check before running live").set_defaults(
        fn=cmd_preflight
    )
    sub.add_parser(
        "paper-proof",
        help="M18 lite: verify paper path (sizing, calendar, fill→lot schema, Discord)",
    ).set_defaults(fn=cmd_paper_proof)
    sub.add_parser(
        "calendar", help="refresh data/calendar.json earnings dates from Yahoo",
    ).set_defaults(fn=cmd_calendar)
    sub.add_parser("metrics", help="dashboard-ready metrics snapshot (JSON)").set_defaults(
        fn=cmd_metrics
    )
    sub.add_parser("watchdog", help="check daemon health, alert if stale").set_defaults(
        fn=cmd_watchdog
    )
    sub.add_parser(
        "notify-test", help="send a one-shot Discord ping to verify the webhook",
    ).set_defaults(fn=cmd_notify_test)
    sub.add_parser("backup", help="back up the journal database").set_defaults(fn=cmd_backup)

    sub.add_parser("status", help="kill switch / budgets / queue").set_defaults(fn=cmd_status)
    sub.add_parser("reset-kill-switch", help="manually reset the kill switch").set_defaults(
        fn=cmd_reset_kill_switch
    )

    args = p.parse_args(argv)
    try:
        return args.fn(args)
    except RuntimeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())

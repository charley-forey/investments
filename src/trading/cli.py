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
    print(f"Anthropic cost (24h): ${journal.cost_since(day_ago):.2f}")
    return 0


def cmd_metrics(_args) -> int:
    import json

    from .monitoring import metrics_snapshot

    print(json.dumps(metrics_snapshot(get_config(), _journal()), indent=2))
    return 0


def cmd_preflight(_args) -> int:
    from .preflight import run_preflight

    result = run_preflight(get_config())
    print(result.report())
    return 0 if result.critical_ok else 1


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


def cmd_stats(_args) -> int:
    from .analytics.lifecycle import stages_summary
    from .analytics.stats import portfolio_summary

    config = get_config()
    journal = _journal()
    print(portfolio_summary(journal, config.settings.tax))
    print("\nStrategy stages: " + stages_summary(journal))
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


def cmd_backtest(args) -> int:
    from backtest.engine import run_backtest
    from backtest.strategies import breakout, sma_crossover

    bars = _load_bars_for(args.symbol, args.days)
    if not bars:
        print(f"no bars for {args.symbol}")
        return 1
    signal = sma_crossover() if args.strategy == "sma" else breakout()

    if args.walkforward:
        from backtest.walkforward import gate_strategy, walk_forward

        wf = walk_forward(bars, signal)
        print(f"{args.symbol} {args.strategy} walk-forward: {wf.summary()} "
              f"-> {'PASS' if wf.passed() else 'FAIL'}")
        if args.tag:
            print("  " + gate_strategy(_journal(), args.tag, wf))
        return 0

    result = run_backtest(bars, signal)
    print(f"{args.symbol} {args.strategy} over {len(bars)} bars: {result.summary()}")

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
    from .web.app import create_app

    print(f"observability dashboard on http://{args.host}:{args.port}")
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

    bt = sub.add_parser("backtest", help="backtest a reference strategy on a symbol")
    bt.add_argument("symbol")
    bt.add_argument("--strategy", choices=["sma", "breakout"], default="sma")
    bt.add_argument("--days", type=int, default=365)
    bt.add_argument("--tag", default=None, help="strategy tag to gate/promote")
    bt.add_argument("--promote", action="store_true",
                    help="promote candidate->paper if expectancy is positive")
    bt.add_argument("--walkforward", action="store_true",
                    help="run out-of-sample walk-forward validation + auto-gate")
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

    sub.add_parser("allocate", help="capital allocation + P&L attribution").set_defaults(
        fn=cmd_allocate
    )

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
    sub.add_parser("preflight", help="go/no-go self-check before running live").set_defaults(
        fn=cmd_preflight
    )
    sub.add_parser("metrics", help="dashboard-ready metrics snapshot (JSON)").set_defaults(
        fn=cmd_metrics
    )
    sub.add_parser("watchdog", help="check daemon health, alert if stale").set_defaults(
        fn=cmd_watchdog
    )
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

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
    from datetime import datetime, timezone

    from .monitoring import check_health

    health = check_health(journal)
    print(f"health: {health.summary()}")
    last = journal.last_successful_cycle()
    if last:
        print(f"last successful cycle: {last['ts']} ({last['detail']})")
    day_ago = (now - timedelta(days=1)).isoformat()
    print(f"Anthropic cost (24h): ${journal.cost_since(day_ago):.2f}")
    return 0


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


def cmd_backtest(args) -> int:
    from backtest.engine import bars_from_alpaca_df, run_backtest
    from backtest.strategies import breakout, sma_crossover

    broker = _broker()
    df = broker.get_bars(args.symbol, days=args.days)
    if df is None or len(df) == 0:
        print(f"no bars for {args.symbol}")
        return 1
    bars = bars_from_alpaca_df(df)
    signal = sma_crossover() if args.strategy == "sma" else breakout()
    result = run_backtest(bars, signal)
    print(f"{args.symbol} {args.strategy} over {len(bars)} bars: {result.summary()}")

    if args.promote and args.tag:
        from .analytics.lifecycle import promote_after_backtest

        change = promote_after_backtest(_journal(), args.tag, result.expectancy)
        if change:
            print(f"  promoted {change.tag}: {change.old_stage} -> {change.new_stage} "
                  f"({change.reason})")
        else:
            print(f"  no promotion for '{args.tag}' (stage unchanged or expectancy <= 0)")
    return 0


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
    bt.add_argument("--tag", default=None, help="strategy tag to promote on a passing backtest")
    bt.add_argument("--promote", action="store_true",
                    help="promote candidate->paper if expectancy is positive")
    bt.set_defaults(fn=cmd_backtest)

    sub.add_parser("sync", help="sync fills and tax lots from the broker").set_defaults(fn=cmd_sync)
    sub.add_parser("daemon", help="run the scheduled trading daemon").set_defaults(fn=cmd_daemon)
    sub.add_parser("stream", help="run the real-time fill websocket").set_defaults(fn=cmd_stream)
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

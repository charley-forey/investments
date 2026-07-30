"""APScheduler daemon. Each job is wrapped so a single failure heartbeats an
error and never kills the scheduler. Uses the market clock so cycles no-op
cleanly on holidays (the orchestrator's intraday skip handles closed markets)."""

from __future__ import annotations

import logging

from .agents.client import make_client
from .broker.alpaca import AlpacaBroker
from .config import Config, get_config
from .data.journal import Journal
from .orchestrator import Orchestrator

log = logging.getLogger("trading.scheduler")


def _build(config: Config) -> Orchestrator:
    journal = Journal(config.settings.paths.journal_db)
    broker = AlpacaBroker(config)
    client = make_client(config)
    return Orchestrator(config, journal, broker, client)


def run_cycle_safe(cycle: str) -> None:
    config = get_config()
    journal = Journal(config.settings.paths.journal_db)
    try:
        orch = _build(config)
        report = orch.run_cycle(cycle)
        log.info(report.summary())
    except Exception as e:  # noqa: BLE001 — daemon must survive any cycle failure
        log.exception("cycle %s failed", cycle)
        journal.heartbeat(f"cycle:{cycle}", status="error", detail=str(e))
        # Surviving the failure quietly is how a dead system looks healthy. On
        # 2026-07-24 the Anthropic credit balance ran out at 12:32 EDT and every
        # cycle failed for the last 3.5 hours of the session with no alert — the
        # postclose review and lesson extraction silently never ran.
        try:
            from .notify import notify_event
            notify_event(config, journal, f"cycle {cycle} FAILED", str(e)[:400])
        except Exception:
            log.exception("could not send failure notification")
    finally:
        journal.close()


def run_protect_safe() -> None:
    """Backstop: every position carries a live GTC stop. Runs at daemon start and
    before the close, so a cancelled/expired bracket leg can't leave a position
    naked overnight."""
    from .broker.sync import ensure_protective_stops

    config = get_config()
    journal = Journal(config.settings.paths.journal_db)
    try:
        n = ensure_protective_stops(config, journal, AlpacaBroker(config))
        log.info("protective stops: attached %d", n)
    except Exception:
        log.exception("protective stop sweep failed")
        journal.heartbeat("protective_stops", status="error", detail="sweep failed")
    finally:
        journal.close()


def run_watchdog_safe() -> None:
    from .monitoring import run_watchdog

    config = get_config()
    journal = Journal(config.settings.paths.journal_db)
    try:
        run_watchdog(config, journal)
    except Exception:
        log.exception("watchdog failed")
    finally:
        journal.close()


def run_daily_summary_safe() -> None:
    from .monitoring import daily_summary

    config = get_config()
    journal = Journal(config.settings.paths.journal_db)
    try:
        daily_summary(config, journal)
    except Exception:
        log.exception("daily summary failed")
    finally:
        journal.close()


def run_backup_safe() -> None:
    from .backup import backup_journal

    config = get_config()
    try:
        dest = backup_journal(config)
        log.info("journal backed up to %s", dest)
    except Exception:
        log.exception("backup failed")


def run_intel_safe() -> None:
    from .broker.alpaca import AlpacaBroker
    from .data.ingest import ingest_intel
    from .data.intel import IntelStore

    config = get_config()
    store = IntelStore(config.settings.paths.intel_db)
    try:
        report = ingest_intel(config, store, AlpacaBroker(config))
        log.info("intel ingest: news+%d social+%d sentiment+%d",
                 report.news_saved, report.social_saved, report.sentiment_snapshots)
    except Exception:
        log.exception("intel ingest failed")
    finally:
        store.close()


def run_movers_safe() -> None:
    from .broker.alpaca import AlpacaBroker
    from .scanner.movers import run_movers_scan

    config = get_config()
    journal = Journal(config.settings.paths.journal_db)
    try:
        report = run_movers_scan(config, AlpacaBroker(config), journal=journal)
        log.info("movers: screened=%d candidates=%d", report.screened, report.candidates)
    except Exception:
        log.exception("movers scan failed")
        journal.heartbeat("movers", status="error", detail="scan failed")
    finally:
        journal.close()


def run_scanner_learning_safe() -> None:
    from .scanner.learning import run_scanner_learning

    config = get_config()
    journal = Journal(config.settings.paths.journal_db)
    try:
        report = run_scanner_learning(config, journal)
        log.info("scanner learning: %s", report.detail)
    except Exception:
        log.exception("scanner learning failed")
    finally:
        journal.close()


def run_snapshot_safe() -> None:
    """Per-interval signal snapshot of the whole universe (no LLM) — the learning
    ledger candidate_grading scores by forward return.

    Its own job because the intraday cycle now runs every minute to drain the tick
    stream's wake queue, and 88 quotes + 88 rows a minute is not a snapshot, it's a
    firehose. Regime tags move on the scale of hours, so this cadence is plenty."""
    from .analytics.snapshot import snapshot_universe

    config = get_config()
    journal = Journal(config.settings.paths.journal_db)
    try:
        n = snapshot_universe(config, journal, AlpacaBroker(config), cycle="intraday")
        log.info("universe snapshot: %d rows", n)
    except Exception:
        log.exception("universe snapshot failed")
    finally:
        journal.close()


def run_signal_grading_safe() -> None:
    """Shadow-grade matured scanner candidates by forward return (no LLM, no
    capital) — the sample-widening half of the learning loop."""
    from .analytics.candidate_grading import grade_pending_candidates

    config = get_config()
    journal = Journal(config.settings.paths.journal_db)
    try:
        report = grade_pending_candidates(journal, AlpacaBroker(config))
        log.info("candidate grading: graded=%d right=%d wrong=%d skipped=%d",
                 report.graded, report.right, report.wrong, report.skipped)
    except Exception:
        log.exception("candidate grading failed")
        journal.heartbeat("candidate_grading", status="error", detail="grading failed")
    finally:
        journal.close()


def run_autocalibrate_safe() -> None:
    """Turn the graded ledger into bounded parameter changes (wake score, per-
    strategy sizing). Gated, clamped, dry-run-able — see analytics/autocalibrate."""
    from .analytics.autocalibrate import run_autocalibrate

    config = get_config()
    journal = Journal(config.settings.paths.journal_db)
    try:
        changes = run_autocalibrate(config, journal)
        log.info("autocalibrate: %d change(s)", len(changes))
    except Exception:
        log.exception("autocalibrate failed")
        journal.heartbeat("autocalibrate", status="error", detail="run failed")
    finally:
        journal.close()


def run_backtest_sweep_safe() -> None:
    """Replay every registered strategy over the stored bars and gate on the result.

    Deliberately after grading (17:15) and autocalibrate (17:25): those consume the
    day's live evidence, this consumes ten years of it."""
    from .analytics.sweep import run_sweep

    config = get_config()
    journal = Journal(config.settings.paths.journal_db)
    try:
        report = run_sweep(config, journal)
        _write_sweep_memory(config, report)
        log.info("backtest sweep: %d strategies, %d promoted",
                 len(report.results), len(report.promoted))
    except Exception:
        log.exception("backtest sweep failed")
        journal.heartbeat("backtest_sweep", status="error", detail="run failed")
    finally:
        journal.close()


def _write_sweep_memory(config, report) -> None:
    """Into memory/ so the premarket agent reads it via read_memory."""
    from datetime import datetime, timezone
    from pathlib import Path

    stamp = datetime.now(timezone.utc).isoformat(timespec="minutes")
    body = [f"# backtest sweep — {stamp}", "", report.as_markdown(), ""]
    if report.promoted:
        body.append(f"Promoted to `paper` this run: {', '.join(report.promoted)}")
    body.append("")
    body.append("PASS = positive mean out-of-sample R across walk-forward folds and "
                "positive on >=60% of symbols. A validated strategy trades at full "
                "size; an unproven one at 25%.")
    regimes = report.regime_markdown()
    if regimes:
        body += ["", "## Alpha per trade by market regime (R vs an exposure-matched "
                 "passive hold; trade count in parens)", "", regimes, "",
                 "Live position sizing is scaled by these: a strategy is cut toward "
                 "25% in regimes where it measured negative, full size where positive. "
                 "The regime is read off SPY with the same classifier the live path "
                 "uses, so history and today cannot disagree."]
    mem = Path(config.settings.paths.memory_dir)
    mem.mkdir(parents=True, exist_ok=True)
    (mem / "backtest_sweep.md").write_text("\n".join(body) + "\n", encoding="utf-8")


def run_bar_ingest_safe() -> None:
    """Keep the local bar history current so the sweep runs offline and free."""
    from .broker.alpaca import AlpacaBroker
    from .data.bars import BarStore, ingest_symbol
    from .scanner.universe import load_screen_universe

    config = get_config()
    store = BarStore(config.settings.paths.bars_db)
    journal = Journal(config.settings.paths.journal_db)
    try:
        broker = AlpacaBroker(config)
        symbols = load_screen_universe().symbols or config.settings.universe.core
        total = 0
        for sym in symbols:
            try:
                total += ingest_symbol(store, broker, sym, days=3650)
            except Exception:
                log.warning("bar ingest failed for %s", sym)
        log.info("bar ingest: %d bars over %d symbols", total, len(symbols))
        journal.heartbeat("bar_ingest", status="ok",
                          detail=f"{total} bars, {len(symbols)} symbols")
    except Exception:
        log.exception("bar ingest failed")
        journal.heartbeat("bar_ingest", status="error", detail="run failed")
    finally:
        store.close()
        journal.close()


def run_calendar_safe() -> None:
    from .data.calendar_feed import refresh_calendar
    from .data.journal import Journal

    config = get_config()
    journal = Journal(config.settings.paths.journal_db)
    try:
        report = refresh_calendar(config)
        log.info("calendar refresh: %d events (%d symbols ok, %d failed)",
                 report.events_written, report.symbols_ok, report.symbols_failed)
        journal.heartbeat(
            "calendar", status="ok",
            detail=f"events={report.events_written} ok={report.symbols_ok} "
                   f"fail={report.symbols_failed}",
        )
    except Exception as e:
        log.exception("calendar refresh failed")
        journal.heartbeat("calendar", status="error", detail=str(e))
    finally:
        journal.close()


def build_scheduler():
    from apscheduler.schedulers.blocking import BlockingScheduler
    from apscheduler.triggers.cron import CronTrigger

    config = get_config()
    sched = config.settings.schedule
    scheduler = BlockingScheduler(timezone=sched.timezone)

    pre_h, pre_m = sched.premarket_research.split(":")
    scheduler.add_job(
        run_cycle_safe, CronTrigger(day_of_week="mon-fri", hour=pre_h, minute=pre_m),
        args=["premarket"], id="premarket", max_instances=1,
    )
    # Refresh earnings calendar 15 minutes before premarket research.
    pre_total = int(pre_h) * 60 + int(pre_m)
    cal_total = max(0, pre_total - 15)
    scheduler.add_job(
        run_calendar_safe,
        CronTrigger(day_of_week="mon-fri", hour=cal_total // 60, minute=cal_total % 60),
        id="calendar", max_instances=1,
    )
    scheduler.add_job(
        run_cycle_safe,
        CronTrigger(day_of_week="mon-fri", hour="9-15",
                    minute=f"*/{sched.intraday_scan_every_minutes}"),
        args=["intraday"], id="intraday", max_instances=1,
    )
    post_h, post_m = sched.postclose_review.split(":")
    scheduler.add_job(
        run_cycle_safe, CronTrigger(day_of_week="mon-fri", hour=post_h, minute=post_m),
        args=["postclose"], id="postclose", max_instances=1,
    )
    wk_h, wk_m = sched.weekend_research_time.split(":")
    scheduler.add_job(
        run_cycle_safe,
        CronTrigger(day_of_week=sched.weekend_research_day, hour=wk_h, minute=wk_m),
        args=["weekend"], id="weekend", max_instances=1,
    )
    # Protective-stop backstop. At the OPEN as well as before the close: on
    # 2026-07-29 the agent spent three proposals and nine minutes hand-building a
    # stop for a position that had been unprotected since the previous session,
    # because the only sweep ran at 15:55 — six hours after it noticed.
    scheduler.add_job(run_protect_safe,
                      CronTrigger(day_of_week="mon-fri", hour="9", minute="32"),
                      id="protect_open", max_instances=1)
    scheduler.add_job(run_protect_safe,
                      CronTrigger(day_of_week="mon-fri", hour="15", minute="55"),
                      id="protect", max_instances=1)
    # Liveness: watchdog every 30 min; daily summary after the close; nightly backup.
    scheduler.add_job(run_watchdog_safe, CronTrigger(minute="*/30"),
                      id="watchdog", max_instances=1)
    scheduler.add_job(run_daily_summary_safe,
                      CronTrigger(day_of_week="mon-fri", hour="16", minute="45"),
                      id="daily_summary", max_instances=1)
    # Backup at 18:05, not 23:30. The machine running this is not up at 23:30 --
    # the daemon is restarted each morning ~07:42 and the last backup on disk was
    # 2026-07-27 despite the job being scheduled nightly. A backup that only runs
    # when someone happens to leave the machine on is not a backup.
    scheduler.add_job(run_backup_safe, CronTrigger(hour="18", minute="5"),
                      id="backup", max_instances=1)
    # Shadow-grade matured scanner candidates nightly (after the close, before backup).
    scheduler.add_job(run_signal_grading_safe,
                      CronTrigger(day_of_week="mon-fri", hour="17", minute="15"),
                      id="signal_grading", max_instances=1)
    # Auto-calibrate bounded params from the fresh ledger (right after grading).
    scheduler.add_job(run_autocalibrate_safe,
                      CronTrigger(day_of_week="mon-fri", hour="17", minute="25"),
                      id="autocalibrate", max_instances=1)
    # Refresh the local bar history, then replay every strategy against it. Ingest
    # first so the sweep always sees today's close; both are free and offline-ish.
    scheduler.add_job(run_bar_ingest_safe,
                      CronTrigger(day_of_week="mon-fri", hour="17", minute="30"),
                      id="bar_ingest", max_instances=1)
    scheduler.add_job(run_backtest_sweep_safe,
                      CronTrigger(day_of_week="mon-fri", hour="17", minute="35"),
                      id="backtest_sweep", max_instances=1)
    # Continuous market-intelligence ingestion during extended market hours.
    scheduler.add_job(
        run_intel_safe,
        CronTrigger(day_of_week="mon-fri", hour="8-17",
                    minute=f"*/{sched.intel_every_minutes}"),
        id="intel", max_instances=1)
    # Deterministic movers / OpportunityScore scan (no LLM).
    movers_every = getattr(sched, "movers_every_minutes", None) or 15
    scheduler.add_job(
        run_snapshot_safe,
        CronTrigger(day_of_week="mon-fri", hour="9-15", minute=f"*/{movers_every}"),
        id="snapshot", max_instances=1)
    scheduler.add_job(
        run_movers_safe,
        CronTrigger(day_of_week="mon-fri", hour="9-15",
                    minute=f"*/{movers_every}"),
        id="movers", max_instances=1)
    # Weekly scanner weight retune + core promote/demote (after weekend research).
    scheduler.add_job(
        run_scanner_learning_safe,
        CronTrigger(day_of_week=sched.weekend_research_day, hour=wk_h,
                    minute=min(59, int(wk_m) + 30)),
        id="scanner_learning", max_instances=1)
    return scheduler


def run_daemon() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    scheduler = build_scheduler()
    run_protect_safe()  # never start the daemon with an unprotected position
    # Announce liveness immediately. The watchdog only ticks every 30 minutes, so
    # without this a freshly restarted daemon reports UNHEALTHY off a heartbeat
    # that predates the restart — which is exactly what a machine waking from
    # sleep looks like, and it cried wolf for half an hour every time.
    try:
        j = Journal(get_config().settings.paths.journal_db)
        j.heartbeat("daemon", detail=f"started; {len(scheduler.get_jobs())} jobs")
        j.close()
    except Exception:
        log.exception("startup heartbeat failed")
    log.info("scheduler starting; jobs: %s", [j.id for j in scheduler.get_jobs()])
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        log.info("scheduler stopped")
    return 0

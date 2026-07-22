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
    # Protective-stop backstop 5 minutes before the close.
    scheduler.add_job(run_protect_safe,
                      CronTrigger(day_of_week="mon-fri", hour="15", minute="55"),
                      id="protect", max_instances=1)
    # Liveness: watchdog every 30 min; daily summary after the close; nightly backup.
    scheduler.add_job(run_watchdog_safe, CronTrigger(minute="*/30"),
                      id="watchdog", max_instances=1)
    scheduler.add_job(run_daily_summary_safe,
                      CronTrigger(day_of_week="mon-fri", hour="16", minute="45"),
                      id="daily_summary", max_instances=1)
    scheduler.add_job(run_backup_safe, CronTrigger(hour="23", minute="30"),
                      id="backup", max_instances=1)
    # Continuous market-intelligence ingestion during extended market hours.
    scheduler.add_job(
        run_intel_safe,
        CronTrigger(day_of_week="mon-fri", hour="8-17",
                    minute=f"*/{sched.intel_every_minutes}"),
        id="intel", max_instances=1)
    # Deterministic movers / OpportunityScore scan (no LLM).
    movers_every = getattr(sched, "movers_every_minutes", None) or sched.intraday_scan_every_minutes
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
    log.info("scheduler starting; jobs: %s", [j.id for j in scheduler.get_jobs()])
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        log.info("scheduler stopped")
    return 0

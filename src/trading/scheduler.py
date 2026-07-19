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
    return scheduler


def run_daemon() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    scheduler = build_scheduler()
    log.info("scheduler starting; jobs: %s", [j.id for j in scheduler.get_jobs()])
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        log.info("scheduler stopped")
    return 0

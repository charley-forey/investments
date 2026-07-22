"""M5: retry/backoff, cost estimation, health/watchdog, and journal backup."""

from datetime import datetime, timedelta, timezone

import pytest

from conftest import make_config

from trading.backup import backup_journal
from trading.cost import Usage, estimate_cost, usage_from_response
from trading.data.journal import Journal
from trading.monitoring import check_health
from trading.resilience import RetryConfig, with_retry


class TestRetry:
    def test_succeeds_first_try(self):
        calls = []
        result = with_retry(lambda: calls.append(1) or "ok",
                            config=RetryConfig(retries=3), sleep=lambda d: None)
        assert result == "ok"
        assert len(calls) == 1

    def test_retries_then_succeeds(self):
        state = {"n": 0}

        def flaky():
            state["n"] += 1
            if state["n"] < 3:
                raise ConnectionError("transient")
            return "recovered"

        result = with_retry(flaky, config=RetryConfig(retries=3), sleep=lambda d: None)
        assert result == "recovered"
        assert state["n"] == 3

    def test_reraises_after_exhaustion(self):
        def always_fail():
            raise TimeoutError("nope")

        with pytest.raises(TimeoutError):
            with_retry(always_fail, config=RetryConfig(retries=2), sleep=lambda d: None)

    def test_backoff_delays_grow(self):
        delays = []

        def fail():
            raise ConnectionError()

        with pytest.raises(ConnectionError):
            with_retry(fail, config=RetryConfig(retries=3, base_delay=1.0, factor=2.0),
                       sleep=lambda d: delays.append(d))
        assert delays == [1.0, 2.0, 4.0]

    def test_only_retries_configured_exceptions(self):
        def fail_value():
            raise ValueError("logic bug")

        with pytest.raises(ValueError):
            with_retry(fail_value, config=RetryConfig(retries=3),
                       retry_on=(ConnectionError,), sleep=lambda d: None)


class TestCost:
    def test_opus_pricing(self):
        # 1M input, 1M output on opus-4-8 = $5 + $25 = $30
        usage = Usage(input_tokens=1_000_000, output_tokens=1_000_000)
        assert estimate_cost(usage, "claude-opus-4-8") == pytest.approx(30.0)

    def test_cache_reads_discounted(self):
        # 1M input all cache-read -> billed at 0.1x input rate = $0.50
        usage = Usage(input_tokens=1_000_000, cache_read_tokens=1_000_000)
        assert estimate_cost(usage, "claude-opus-4-8") == pytest.approx(0.5)

    def test_unknown_model_falls_back(self):
        usage = Usage(input_tokens=1_000_000)
        assert estimate_cost(usage, "some-future-model") == pytest.approx(5.0)

    def test_usage_from_response_tolerates_missing(self):
        class R:  # no usage attr
            pass
        assert usage_from_response(R()).input_tokens == 0


class TestHealth:
    def test_no_heartbeats_is_unhealthy(self, tmp_path):
        journal = Journal(tmp_path / "j.db")
        h = check_health(journal)
        assert not h.healthy
        assert "no heartbeats" in h.reason

    def test_fresh_heartbeat_is_healthy(self, tmp_path):
        journal = Journal(tmp_path / "j.db")
        journal.heartbeat("cycle:intraday", status="end", detail="ok")
        h = check_health(journal, max_stale_minutes=60)
        assert h.healthy

    def test_stale_heartbeat_is_unhealthy(self, tmp_path):
        journal = Journal(tmp_path / "j.db")
        journal.heartbeat("cycle:intraday", status="end")
        future = datetime.now(timezone.utc) + timedelta(hours=3)
        h = check_health(journal, max_stale_minutes=60, now=future)
        assert not h.healthy
        assert "stale" in h.reason

    def test_recent_errors_flagged_but_alive(self, tmp_path):
        journal = Journal(tmp_path / "j.db")
        journal.heartbeat("cycle:intraday", status="error", detail="boom")
        journal.heartbeat("watchdog", status="ok")
        h = check_health(journal, max_stale_minutes=60)
        assert h.healthy
        assert "error" in h.reason

    def test_reconcile_halt_is_unhealthy(self, tmp_path):
        journal = Journal(tmp_path / "j.db")
        journal.heartbeat("cycle:intraday", status="end", detail="ok")
        journal.set_state("reconcile_halt", "AAPL: broker=15 journal_lots=0")
        h = check_health(journal, max_stale_minutes=60)
        assert not h.healthy
        assert "reconcile halt" in h.reason

    def test_kill_switch_is_unhealthy(self, tmp_path):
        journal = Journal(tmp_path / "j.db")
        journal.heartbeat("cycle:intraday", status="end", detail="ok")
        journal.trip_kill_switch("daily loss")
        h = check_health(journal, max_stale_minutes=60)
        assert not h.healthy
        assert "kill switch" in h.reason


class TestBackup:
    def test_backup_creates_and_rotates(self, tmp_path):
        config = make_config()
        config.settings.paths.journal_db = str(tmp_path / "data" / "journal.db")
        journal = Journal(config.settings.paths.journal_db)
        journal.heartbeat("test")
        journal.close()

        made = [backup_journal(config, keep=3) for _ in range(5)]
        # each returns a path that exists
        assert all(p.exists() for p in made[-3:])
        backups = list((tmp_path / "data" / "backups").glob("journal-*.db"))
        assert len(backups) == 3  # rotated to keep=3

    def test_backup_is_readable_sqlite(self, tmp_path):
        config = make_config()
        config.settings.paths.journal_db = str(tmp_path / "data" / "journal.db")
        journal = Journal(config.settings.paths.journal_db)
        journal.heartbeat("marker", detail="findme")
        journal.close()

        dest = backup_journal(config)
        restored = Journal(dest)
        hbs = restored.recent_heartbeats(10)
        assert any(h["detail"] == "findme" for h in hbs)


class TestCostTrackingInJournal:
    def test_record_and_sum_cost(self, tmp_path):
        journal = Journal(tmp_path / "j.db")
        journal.record_usage(cycle="intraday", agent="strategy", model="claude-opus-4-8",
                            input_tokens=1000, output_tokens=500, cache_read_tokens=0,
                            cost_usd=0.02)
        journal.record_usage(cycle="postclose", agent="scoring", model="claude-opus-4-8",
                            input_tokens=800, output_tokens=200, cache_read_tokens=0,
                            cost_usd=0.01)
        day_ago = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        assert journal.cost_since(day_ago) == pytest.approx(0.03)

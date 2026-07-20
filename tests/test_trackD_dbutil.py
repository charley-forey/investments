"""Tests for the concurrent-safe sqlite helpers."""

import sqlite3

import pytest

from trading.data.db_util import open_connection, with_write_retry


def test_open_connection_sets_wal_and_busy_timeout(tmp_path):
    conn = open_connection(tmp_path / "sub" / "j.db", busy_timeout_ms=3000)
    assert conn.row_factory is sqlite3.Row
    assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 3000
    # parent dir was created for us
    assert (tmp_path / "sub" / "j.db").exists()
    conn.close()


def test_lock_raises_without_retry(tmp_path):
    """Prove the lock path is real: a second writer with a tiny busy_timeout
    hits 'database is locked' while the first holds an exclusive write lock."""
    db = tmp_path / "j.db"
    a = open_connection(db)
    a.execute("CREATE TABLE t (x)")
    a.commit()
    b = open_connection(db, busy_timeout_ms=10)
    a.execute("BEGIN IMMEDIATE")
    a.execute("INSERT INTO t VALUES (1)")  # a holds the write lock
    with pytest.raises(sqlite3.OperationalError) as exc:
        b.execute("INSERT INTO t VALUES (2)")
        b.commit()
    assert "database is locked" in str(exc.value).lower()
    a.commit()
    a.close()
    b.close()


def test_with_write_retry_recovers_when_lock_clears(tmp_path):
    """First attempt collides with a held lock; retry succeeds once the lock
    is released between attempts."""
    db = tmp_path / "j.db"
    a = open_connection(db)
    a.execute("CREATE TABLE t (x)")
    a.commit()
    b = open_connection(db, busy_timeout_ms=10)
    a.execute("BEGIN IMMEDIATE")
    a.execute("INSERT INTO t VALUES (1)")  # a holds the lock

    calls = []

    def writer():
        calls.append(1)
        if len(calls) == 2:
            a.commit()  # release the lock just before this attempt's write
        b.execute("INSERT INTO t VALUES (2)")
        b.commit()
        return "ok"

    result = with_write_retry(writer, attempts=5, base_delay=0.001)
    assert result == "ok"
    assert len(calls) == 2  # failed once, succeeded on retry
    assert b.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 2
    a.close()
    b.close()


def test_with_write_retry_reraises_non_lock_error():
    def boom():
        raise sqlite3.OperationalError("no such table: nope")

    with pytest.raises(sqlite3.OperationalError):
        with_write_retry(boom, attempts=3, base_delay=0.001)


def test_with_write_retry_gives_up_after_attempts():
    calls = []

    def always_locked():
        calls.append(1)
        raise sqlite3.OperationalError("database is locked")

    with pytest.raises(sqlite3.OperationalError):
        with_write_retry(always_locked, attempts=3, base_delay=0.001)
    assert len(calls) == 3

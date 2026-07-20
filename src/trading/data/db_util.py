"""SQLite connection helpers for concurrent access.

The journal DB is opened by the daemon, the dashboard, and one-off CLI runs at
the same time. WAL lets readers and one writer coexist, but two writers still
collide as ``sqlite3.OperationalError: database is locked``. Route every opener
through :func:`open_connection` (busy_timeout makes writers *wait* on the lock
instead of raising immediately) and wrap multi-statement writes in
:func:`with_write_retry` for the rare case the timeout still lapses.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Callable, TypeVar

T = TypeVar("T")


def open_connection(path: str | Path, *, busy_timeout_ms: int = 5000) -> sqlite3.Connection:
    """Open a sqlite3 connection configured for safe concurrent use:
    Row factory, WAL journal mode, and a busy timeout so a writer blocked by
    another writer waits up to ``busy_timeout_ms`` rather than raising at once.

    ``busy_timeout`` is per-connection, so this must run on every opener.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=max(busy_timeout_ms, 0) / 1000.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(f"PRAGMA busy_timeout={int(max(busy_timeout_ms, 0))}")
    return conn


def with_write_retry(
    fn: Callable[[], T], *, attempts: int = 5, base_delay: float = 0.05
) -> T:
    """Run ``fn`` (a write) retrying only on "database is locked". Backs off
    exponentially (base_delay, 2x, 4x, ...) between attempts. Re-raises the last
    lock error after exhausting attempts, and re-raises any other error at once.
    """
    attempts = max(attempts, 1)
    for attempt in range(attempts):
        try:
            return fn()
        except sqlite3.OperationalError as e:
            if "database is locked" not in str(e).lower() or attempt == attempts - 1:
                raise
            time.sleep(base_delay * (2 ** attempt))
    raise AssertionError("unreachable")  # loop either returns or raises

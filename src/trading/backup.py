"""Nightly journal backup. The SQLite journal is the entire audit trail and tax
record, so it is copied (via SQLite's online backup API, safe while the daemon is
running) into a rotated set of timestamped files."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .config import Config


def backup_journal(config: Config, *, keep: int = 14) -> Path:
    """Create a consistent copy of the journal DB and prune old backups.
    Returns the path of the new backup."""
    db_path = Path(config.settings.paths.journal_db)
    backup_dir = db_path.parent / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
    dest = backup_dir / f"journal-{stamp}.db"

    # Online backup API: consistent snapshot even with the daemon connected.
    src = sqlite3.connect(str(db_path))
    try:
        out = sqlite3.connect(str(dest))
        try:
            src.backup(out)
        finally:
            out.close()
    finally:
        src.close()

    _prune(backup_dir, keep)
    return dest


def _prune(backup_dir: Path, keep: int) -> None:
    backups = sorted(backup_dir.glob("journal-*.db"))
    for old in backups[:-keep] if keep > 0 else []:
        try:
            old.unlink()
        except OSError:
            pass

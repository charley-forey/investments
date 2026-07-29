"""The test fixture must never point at the developer's real data directory.

This is a guard on the fixture itself, not on any feature. `Paths()` defaults are
relative ("data/journal.db") because production resolves them against the project
root in `load_config`; tests build `Config` directly, so those defaults stayed
relative and `.resolve()` turned them into the real `data/` via the cwd.

Nothing failed for weeks, because the leak is invisible on a machine with an empty
`data/`. It only appeared once the scanner had written 8 live candidates:
`test_snapshot_universe_standalone` asserted `10 == 2` (2 core symbols plus the 8 real
ones) and `test_ingest_persists_and_is_idempotent` asserted `9 == 1`. Both tests were
correct; the fixture was lying to them.
"""

from __future__ import annotations

from pathlib import Path

from conftest import make_config

from trading.scanner.movers import candidates_path
from trading.triggers import triggers_path

REPO = Path(__file__).resolve().parents[1]
REAL_DATA = REPO / "data"


def _is_inside(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def test_journal_path_is_not_the_real_data_dir():
    config = make_config()
    assert not _is_inside(Path(config.settings.paths.journal_db), REAL_DATA)


def test_sidecar_files_are_not_the_real_ones():
    """candidates.json and triggers.json are derived from the journal's PARENT
    directory, so they leak even when a test carefully overrides journal_db. This is
    the assertion that actually catches the bug that shipped."""
    config = make_config()
    assert not _is_inside(candidates_path(config), REAL_DATA)
    assert not _is_inside(triggers_path(config), REAL_DATA)


def test_every_data_path_is_isolated():
    p = make_config().settings.paths
    for name in ("journal_db", "bars_dir", "bars_db", "intel_db", "vectors_db",
                 "fundamentals_db", "memory_dir", "calendar_file"):
        assert not _is_inside(Path(getattr(p, name)), REAL_DATA), name


def test_playbooks_still_resolve_to_the_repo():
    """Deliberately NOT isolated: playbooks are version-controlled inputs the agent
    reads, and the registry test asserts every proposable tag has one."""
    p = Path(make_config().settings.paths.playbooks_dir)
    assert p.exists() and (p / "trend-pullback-long.md").exists()


def test_each_test_gets_a_fresh_data_dir(tmp_path):
    """Per-test, not per-session: a shared dir would let one test's candidates.json
    leak into the next, which is the same bug at smaller scale."""
    first = Path(make_config().settings.paths.journal_db).parent
    assert _is_inside(first, tmp_path), "data dir should live under this test's tmp_path"


def test_writing_a_sidecar_does_not_touch_the_repo():
    config = make_config()
    path = candidates_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"candidates": []}', encoding="utf-8")
    assert path.exists()
    assert not _is_inside(path, REAL_DATA)

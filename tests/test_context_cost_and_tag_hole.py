"""Cost of thinking, and the hole retired tags slipped through.

2026-07-30 cut sessions 60% (128 -> 51) but cost only 7% ($15.07 -> $14.03),
because cost PER SESSION went $0.116 -> $0.274. The driver was measured, not
guessed: `get_options_chain` went from 0 calls to 50 across 51 sessions, and the
two unbounded directory dumps ran every cycle beside it.

    read_memory        3,402 tokens x 51 sessions
    get_options_chain  2,324 tokens x 50 sessions
    read_playbook      3,733 tokens (ALL nine playbooks, to consider one)

Separately, `validate_tag` exempted options entirely. `propose_order` accepts
asset_class="option" with hand-built legs and a free-text tag, so proposal #37 was
SUBMITTED under `relative-strength-long` -- deleted for grading -$901, and
rejected on every stock proposal. The registry exists to stop exactly that.
"""

import pytest

from trading import strategies as registry


# -- the tag hole -------------------------------------------------------------

def test_a_retired_tag_is_rejected_on_options():
    """The exact 2026-07-30 proposal #37."""
    err = registry.validate_tag("relative-strength-long", "option")
    assert err and "relative-strength-long" in err


def test_a_retired_tag_is_still_rejected_on_stock():
    assert registry.validate_tag("news-impulse", "stock")


def test_synthesized_vertical_tags_are_accepted():
    """propose_vertical builds these; they are legitimate and unbacktestable."""
    for tag in ("debit-call-vertical", "credit-put-vertical",
                "debit-put-vertical", "credit-call-vertical"):
        assert registry.validate_tag(tag, "option") is None, tag


def test_registry_tags_are_accepted_on_options():
    """An option expressing a registered strategy's thesis keeps its tag, so the
    grading ledger can attribute it."""
    assert registry.validate_tag("vol-premium", "option") is None
    assert registry.validate_tag("breakout", "option") is None


def test_an_invented_option_tag_is_rejected():
    assert registry.validate_tag("earnings-moonshot", "option")


# -- context size -------------------------------------------------------------

def test_playbook_reads_one_strategy_not_all_of_them(config, journal, tmp_path):
    from trading.tools.registry import ToolRegistry

    books = tmp_path / "pb"
    books.mkdir()
    for name in ("breakout", "vol-premium", "mean-reversion"):
        (books / f"{name}.md").write_text("x" * 1500, encoding="utf-8")
    config.settings.paths.playbooks_dir = str(books)

    one = ToolRegistry._t_read_playbook(
        _Ctx(config), {"strategy_tag": "breakout"})
    assert "breakout.md" in one
    assert "vol-premium.md" not in one, "asking for one must not return all"


def test_an_unknown_playbook_lists_what_exists(config, tmp_path):
    from trading.tools.registry import ToolRegistry

    books = tmp_path / "pb"
    books.mkdir()
    (books / "breakout.md").write_text("x", encoding="utf-8")
    config.settings.paths.playbooks_dir = str(books)
    out = ToolRegistry._t_read_playbook(_Ctx(config), {"strategy_tag": "nope"})
    assert "breakout" in out


def test_directory_dumps_are_bounded(config, tmp_path):
    """memory/ is re-read every cycle and grows as the system writes lessons. An
    unbounded read on a one-minute loop is a cost leak with a slow fuse."""
    from trading.tools.registry import ToolRegistry

    mem = tmp_path / "mem"
    mem.mkdir()
    (mem / "huge.md").write_text("y" * 50_000, encoding="utf-8")
    out = ToolRegistry._read_dir(str(mem), "memory")
    assert len(out) < 50_000
    assert "truncated" in out, "truncation must be visible, not silent"


def test_a_small_memory_file_is_untouched(config, tmp_path):
    from trading.tools.registry import ToolRegistry

    mem = tmp_path / "mem"
    mem.mkdir()
    (mem / "small.md").write_text("the whole thing", encoding="utf-8")
    out = ToolRegistry._read_dir(str(mem), "memory")
    assert "the whole thing" in out and "truncated" not in out


class _Ctx:
    """Minimal stand-in: _t_read_playbook only touches ctx.config."""

    def __init__(self, config):
        self.ctx = type("C", (), {"config": config})()


# -- metering -----------------------------------------------------------------

def test_one_hour_cache_writes_are_persisted(journal):
    """The field was added to the cost model and never to the table, so 'is the
    1h TTL helping?' was unanswerable exactly when spend regressed."""
    journal.record_usage(cycle="intraday", agent="strategy", model="m",
                         input_tokens=1, output_tokens=2, cache_read_tokens=3,
                         cost_usd=0.5, cache_write_tokens=4,
                         cache_write_1h_tokens=5)
    row = journal.conn.execute(
        "SELECT cache_write_tokens, cache_write_1h_tokens FROM usage").fetchone()
    assert row["cache_write_tokens"] == 4
    assert row["cache_write_1h_tokens"] == 5


def test_one_hour_writes_bill_at_double_not_1_25x():
    from trading.cost import Usage, split_cost

    five_min = split_cost(Usage(cache_write_tokens=1_000_000), "claude-opus-4-8")[0]
    one_hour = split_cost(Usage(cache_write_1h_tokens=1_000_000), "claude-opus-4-8")[0]
    assert one_hour == pytest.approx(five_min * (2.0 / 1.25))

"""Cost meter accuracy. The daily-spend cap (agents.max_daily_cost_usd) is enforced
against this number, so an undercounting meter silently raises the real ceiling."""

from trading.cost import Usage, estimate_cost, split_cost, usage_from_response


class _FakeUsage:
    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


class _FakeResponse:
    def __init__(self, usage):
        self.usage = usage


def test_cache_creation_tokens_are_billed():
    """Every cycle writes the cached system prompt; those tokens bill at 1.25x
    input and were previously recorded as free."""
    u = Usage(input_tokens=0, output_tokens=0, cache_read_tokens=0, cache_write_tokens=1_000_000)
    # opus-4-8 input is $5/1M -> 1M cache-write tokens = $5 * 1.25
    assert abs(estimate_cost(u, "claude-opus-4-8") - 6.25) < 1e-6


def test_input_tokens_are_not_reduced_by_cache_reads():
    """The API reports input_tokens as the uncached remainder — the three input
    counts are disjoint. Subtracting reads from it double-counted the discount."""
    u = Usage(input_tokens=1_000_000, output_tokens=0, cache_read_tokens=1_000_000)
    # 1M uncached @ $5 + 1M cached @ $0.50 = $5.50 (the old math gave $0.50)
    assert abs(estimate_cost(u, "claude-opus-4-8") - 5.50) < 1e-6


def test_all_four_components_sum():
    u = Usage(input_tokens=100_000, output_tokens=10_000,
              cache_read_tokens=500_000, cache_write_tokens=200_000)
    expected = (
        100_000 * 5 / 1e6          # uncached input
        + 200_000 * 5 * 1.25 / 1e6  # cache write
        + 500_000 * 5 * 0.1 / 1e6   # cache read
        + 10_000 * 25 / 1e6         # output
    )
    assert abs(estimate_cost(u, "claude-opus-4-8") - expected) < 1e-6


def test_split_reconciles_with_the_enforced_total():
    """`trading tokens` decides which half to optimise from split_cost, while the
    daily cap is enforced on estimate_cost. If those two ever disagree we would be
    tuning against one number and being throttled by another."""
    u = Usage(input_tokens=100_000, output_tokens=10_000,
              cache_read_tokens=500_000, cache_write_tokens=200_000)
    for model in ("claude-opus-4-8", "claude-haiku-4-5", "unknown-model"):
        in_cost, out_cost = split_cost(u, model)
        assert abs((in_cost + out_cost) - estimate_cost(u, model)) < 1e-6, model


def test_split_puts_all_three_input_kinds_on_the_input_side():
    """Cache reads and writes are input cost. Attributing them to output would
    point the next optimisation at thinking depth when the fat is in the prompt."""
    u = Usage(input_tokens=1000, output_tokens=0,
              cache_read_tokens=9000, cache_write_tokens=5000)
    in_cost, out_cost = split_cost(u, "claude-opus-4-8")
    assert out_cost == 0.0
    assert in_cost > 0.0


def test_usage_extraction_captures_cache_creation():
    r = _FakeResponse(_FakeUsage(
        input_tokens=10, output_tokens=20,
        cache_read_input_tokens=30, cache_creation_input_tokens=40,
    ))
    u = usage_from_response(r)
    assert (u.input_tokens, u.output_tokens, u.cache_read_tokens, u.cache_write_tokens) == (
        10, 20, 30, 40)


def test_missing_cache_fields_do_not_crash():
    u = usage_from_response(_FakeResponse(_FakeUsage(input_tokens=5, output_tokens=1)))
    assert u.cache_write_tokens == 0 and u.input_tokens == 5


def test_add_accumulates_cache_writes():
    a = Usage(cache_write_tokens=10)
    a.add(Usage(cache_write_tokens=5))
    assert a.cache_write_tokens == 15


def test_new_meter_reads_higher_than_the_old_one():
    """Regression guard for the undercount that made a $8/day cap pass ~$15/day."""
    u = Usage(input_tokens=50_000, output_tokens=5_000,
              cache_read_tokens=400_000, cache_write_tokens=150_000)
    old = (max(0, u.input_tokens - u.cache_read_tokens) * 5 / 1e6
           + u.cache_read_tokens * 5 * 0.1 / 1e6
           + u.output_tokens * 25 / 1e6)
    assert estimate_cost(u, "claude-opus-4-8") > old


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"ok  {name}")
    print("all cost-accounting checks passed")

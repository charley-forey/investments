"""Thin-venue quote sanitisation. Every number below is a real IEX reading taken
during the core session on 2026-07-24, when the system marked, sized and routed off
fabricated prices."""

from trading.broker.models import Quote


def test_usable_book_is_left_alone():
    q = Quote(symbol="SPY", bid=738.28, ask=738.30, last=738.29)
    assert q.book_is_usable
    assert q.effective_book == (738.28, 738.30)
    assert abs(q.mid - 738.29) < 0.01
    assert abs(q.spread - 0.02) < 1e-9


def test_one_sided_book_prices_off_the_trade():
    # Real IEX reading: AAPL bid 312.08, ask 0.00, trade 333.48 -> 6.4% mark error.
    q = Quote(symbol="AAPL", bid=312.08, ask=0.0, last=333.48)
    assert not q.book_is_usable
    assert abs(q.mid - 333.48) < 0.01, "must mark at the trade, not the stale bid"
    assert q.spread < 0.5, "a real market, not the infinite spread of a dead book"


def test_absurd_spread_is_rejected_as_a_market():
    # Real IEX reading: UNH 402.80 / 448.19 = 1067bps. Real UNH trades at ~1bp.
    q = Quote(symbol="UNH", bid=402.80, ask=448.19, last=420.67)
    assert not q.book_is_usable
    assert abs(q.mid - 420.67) < 0.01
    # The bogus book would have implied ~$45/share of cost and no trade could clear
    # a 2x edge hurdle against it.
    assert q.spread < 1.0


def test_no_book_and_no_trade_stays_unpriceable():
    q = Quote(symbol="ZZZZ", bid=0.0, ask=0.0, last=0.0)
    assert q.spread == float("inf"), "unpriceable must fail the cost hurdle, not pass free"
    assert not q.is_two_sided


def test_crossed_book_is_not_a_market():
    q = Quote(symbol="MU", bid=101.0, ask=100.0, last=100.5)
    assert not q.book_is_usable
    assert abs(q.mid - 100.5) < 0.01


def test_synthetic_book_brackets_the_trade_for_routing():
    q = Quote(symbol="NVDA", bid=197.07, ask=0.0, last=206.87)
    bid, ask = q.effective_book
    assert bid < q.last < ask, "routing needs a two-sided book around the real price"
    assert (ask - bid) / q.last * 10_000 <= 10


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"ok  {name}")
    print("all quote-quality checks passed")

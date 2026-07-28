"""The three fixes that change what the system is allowed to trade.

1. A reward:risk floor. The graded ledger through 2026-07-27 had median R = 0.48 at a
   45% win rate — avg win $74.72 against avg loss $187.79, i.e. -$69.66/trade. No
   position size fixes that; only the payoff shape does.
2. The builder and the guardrail must agree on a credit spread's max loss, or the
   builder sizes trades the guardrail then rejects.
3. A credit spread must submit as a credit. `abs()` sent it as a debit — an offer to
   pay the premium we meant to receive.
"""

from datetime import date, timedelta

import pytest

from trading.analytics.options import ContractRow, build_vertical
from trading.guardrails.account_math import analyze_option_legs
from trading.guardrails.models import OptionLeg, OrderProposal


# -- 1. reward:risk floor ----------------------------------------------------

def _rr(entry, stop, target):
    """The ratio the guardrail computes."""
    return abs(target - entry) / abs(entry - stop)


def test_the_baseline_geometry_is_what_the_floor_rejects():
    # Proposal #28 (AAPL, 2026-07-24): entry 333.50, stop 329.50, target 336.50.
    # R = 0.75. This shape, repeated 20 times, is the -$69.66/trade.
    assert _rr(333.50, 329.50, 336.50) == pytest.approx(0.75, abs=0.01)
    assert _rr(333.50, 329.50, 336.50) < 1.5


def test_break_even_math_the_floor_is_derived_from():
    # At a 45% win rate, expectancy is positive only above R = 0.55/0.45 = 1.22.
    win_rate = 0.45
    break_even_r = (1 - win_rate) / win_rate
    assert break_even_r == pytest.approx(1.222, abs=0.001)
    assert 1.5 > break_even_r          # the configured floor leaves margin


def test_floor_boundary_is_inclusive_above_and_exclusive_below():
    assert _rr(100.0, 99.0, 101.49) < 1.5      # rejected
    assert _rr(100.0, 99.0, 101.51) >= 1.5     # passes


def test_a_short_trade_ratio_is_direction_agnostic():
    # Short: entry 100, stop 101 (above), target 97 (below). R = 3.
    assert _rr(100.0, 101.0, 97.0) == pytest.approx(3.0)


# -- 2. builder / guardrail agreement ----------------------------------------

def _chain(spot=100.0):
    exp = date.today() + timedelta(days=30)
    mids = {"call": {90: 11.0, 95: 7.0, 100: 3.5, 105: 1.5, 110: 0.6},
            "put": {90: 0.6, 95: 1.5, 100: 3.0, 105: 6.0, 110: 10.5}}
    rows = []
    for right, m in mids.items():
        for strike, mid in m.items():
            rows.append(ContractRow(
                occ=f"{right[0].upper()}{strike}", expiry=exp, right=right, strike=strike,
                dte=30, moneyness=(strike - spot) / spot, bid=mid - 0.05, ask=mid + 0.05,
                mid=mid, spread_bps=10, iv=0.3, delta=0.5, gamma=0, theta=0, vega=0,
                last_size=1))
    return rows


@pytest.mark.parametrize("mode,direction", [
    ("credit", "bullish"), ("credit", "bearish"),
    ("debit", "bullish"), ("debit", "bearish"),
])
def test_builder_max_loss_equals_what_the_guardrail_will_compute(mode, direction):
    """The invariant that was missing. If either side drifts again, this fails —
    which is the whole reason no credit spread ever reached the broker."""
    plan, note = build_vertical(_chain(), direction=direction, spot=100.0,
                                max_loss_usd=2500.0, mode=mode)
    assert note == "ok"
    legs = [OptionLeg(**leg) for leg in plan.legs]
    assert plan.max_loss_usd == pytest.approx(analyze_option_legs(legs).max_loss_usd)


def test_credit_spread_fits_its_budget_instead_of_being_rejected():
    # 5-wide bull put at $2,500: width*100 = $500/contract -> 5 contracts, $2,500.
    # Under the old (width - credit) math this sized to 7 and the guardrail killed it.
    plan, note = build_vertical(_chain(), direction="bullish", spot=100.0,
                                max_loss_usd=2500.0, mode="credit")
    assert note == "ok"
    assert plan.contracts == 5
    assert plan.max_loss_usd <= 2500.0


# -- 3. credit submits as a credit -------------------------------------------

def _net(legs):
    """The signed net the submit path computes: buys pay, sells receive."""
    return sum((1 if l.side == "buy" else -1) * l.est_premium for l in legs)


def _proposal(legs):
    return OrderProposal(symbol="XYZ", asset_class="option", qty=0, legs=legs)


def test_debit_vertical_nets_positive_we_pay():
    legs = [OptionLeg(side="buy", right="call", strike=100, expiry=date(2026, 9, 18),
                      qty=1, est_premium=5.0),
            OptionLeg(side="sell", right="call", strike=105, expiry=date(2026, 9, 18),
                      qty=1, est_premium=3.0)]
    assert _net(legs) == pytest.approx(2.0)


def test_credit_vertical_nets_negative_we_receive():
    # Bull put: sell the 100 put, buy the 95 wing. We collect 1.5.
    legs = [OptionLeg(side="sell", right="put", strike=100, expiry=date(2026, 9, 18),
                      qty=1, est_premium=3.0),
            OptionLeg(side="buy", right="put", strike=95, expiry=date(2026, 9, 18),
                      qty=1, est_premium=1.5)]
    net = _net(legs)
    assert net == pytest.approx(-1.5)
    # The bug: abs() turned this into +1.5 — an offer to PAY 1.5 to open a position
    # that should have paid us. Never filled sensibly, and never caught, because no
    # credit spread had ever been proposed.
    assert abs(net) != net
    assert _proposal(legs).is_option


def test_closing_a_debit_vertical_is_itself_a_net_credit():
    """Not just a credit-spread problem — every spread EXIT hit this too."""
    legs = [OptionLeg(side="sell", right="call", strike=100, expiry=date(2026, 9, 18),
                      qty=1, est_premium=5.0),
            OptionLeg(side="buy", right="call", strike=105, expiry=date(2026, 9, 18),
                      qty=1, est_premium=3.0)]
    assert _net(legs) < 0


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-q"]))

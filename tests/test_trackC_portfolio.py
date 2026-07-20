"""Track C: portfolio risk + vol-targeted sizing."""

from dataclasses import dataclass

from trading.analytics.portfolio_risk import portfolio_risk
from trading.analytics.sizing import (
    drawdown_throttle, iv_adjusted_size, vol_target_size,
)


@dataclass
class Pos:
    symbol: str
    qty: float
    price: float
    asset_class: str = "stock"
    delta: float = 0.0
    vega: float = 0.0
    underlier: float = 0.0


def test_portfolio_risk_mixed_book():
    equity = 100_000.0
    positions = [
        Pos("AAPL", qty=100, price=200.0),                       # +$20k long
        Pos("SPY", qty=-50, price=400.0),                        # -$20k short
        Pos("TSLA_C", qty=2, price=5.0, asset_class="option",    # 2 calls, 0.5 delta
            delta=0.5, vega=0.10, underlier=250.0),
    ]
    pr = portfolio_risk(positions, equity, betas={"TSLA_C": 2.0})

    # net delta: +20k (AAPL) -20k (SPY) + 0.5*100*2*250 = +25000
    assert abs(pr.net_delta_dollars - 25_000.0) < 1e-6
    # option premium notional = 5 * 2 * 100 = $1000
    # gross = 20k + 20k + 1k = 41k ; net = 20k - 20k + 1k = 1k
    assert abs(pr.gross_exposure - 41_000.0) < 1e-6
    assert abs(pr.net_exposure - 1_000.0) < 1e-6
    assert abs(pr.gross_leverage - 0.41) < 1e-6
    assert abs(pr.net_leverage - 0.01) < 1e-6
    # net vega = 0.10 * 100 * 2 = 20
    assert abs(pr.net_vega - 20.0) < 1e-6
    # largest = 20k / 100k
    assert abs(pr.largest_position_pct - 0.20) < 1e-6
    assert pr.n_positions == 3
    assert "net delta" in pr.summary()


def test_portfolio_risk_defaults_and_zero_equity():
    # unknown beta -> 1.0; zero equity -> leverage 0, no crash
    pr0 = portfolio_risk([Pos("X", qty=10, price=10.0)], 0.0)
    assert pr0.gross_leverage == 0.0
    assert pr0.net_exposure == 100.0

    pr = portfolio_risk([Pos("X", qty=10, price=10.0)], 1000.0)
    # one 100% -weighted long book with beta 1 -> portfolio_beta == net_leverage
    assert abs(pr.portfolio_beta - pr.net_leverage) < 1e-6


def test_portfolio_risk_bad_input_no_raise():
    # missing fields / None qty must not raise
    pr = portfolio_risk([{"symbol": "Z"}, None if False else {"symbol": "Y", "qty": 1, "price": 5}], 500.0)
    assert pr.n_positions == 2


def test_vol_target_higher_vol_smaller_size():
    equity, price = 100_000.0, 100.0
    low_vol = vol_target_size(equity, price, 0.20, target_annual_vol=0.15, max_weight=0.50)
    high_vol = vol_target_size(equity, price, 0.60, target_annual_vol=0.15, max_weight=0.50)
    assert high_vol < low_vol
    # weight = 0.15/0.20 = 0.75 -> capped at max_weight 0.50 -> 500 sh
    assert low_vol == 500
    # weight = 0.15/0.60 = 0.25 -> 250 sh
    assert high_vol == 250


def test_vol_target_capped_by_max_weight():
    # very low vol wants huge weight, but max_weight=0.10 caps it
    qty = vol_target_size(100_000.0, 100.0, 0.05, target_annual_vol=0.15, max_weight=0.10)
    assert qty == 100  # 0.10 * 100k / 100


def test_vol_target_guards():
    assert vol_target_size(0, 100, 0.2) == 0
    assert vol_target_size(100_000, 0, 0.2) == 0
    assert vol_target_size(100_000, 100, 0) == 0


def test_drawdown_throttle_graded_and_clamped():
    assert drawdown_throttle(0.0) == 1.0
    assert drawdown_throttle(-0.02) == 1.0        # gains clamp to full size
    assert drawdown_throttle(0.05) == 1.0         # at soft
    assert drawdown_throttle(0.15) == 0.0         # at hard
    assert drawdown_throttle(0.30) == 0.0         # beyond hard, clamped
    mid = drawdown_throttle(0.10, soft=0.05, hard=0.15)  # halfway
    assert abs(mid - 0.5) < 1e-6
    assert 0.0 <= mid <= 1.0
    # degenerate band doesn't divide by zero
    assert drawdown_throttle(0.10, soft=0.10, hard=0.10) == 0.0


def test_iv_adjusted_size():
    assert iv_adjusted_size(100, None) == 100     # no IV -> no-op
    assert iv_adjusted_size(100, 50.0) == 100     # below high -> no-op
    assert iv_adjusted_size(100, 100.0, high=80.0, floor=0.5) == 50  # max rich -> floor
    assert iv_adjusted_size(100, 90.0, high=80.0, floor=0.5) == 75   # halfway

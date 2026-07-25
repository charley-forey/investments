"""Risk dial: profile overlay scales sizing but never the safety floor; the
aggressive profile is gated by track record and only forceable with a journaled
override."""

from trading.analytics import risk_profile as rp
from trading.config import Paths
from trading.data.journal import Journal

from conftest import make_config


def _config_in(tmp_path):
    config = make_config()
    paths = Paths(journal_db=str(tmp_path / "journal.db")).resolve(tmp_path)
    return config.model_copy(
        update={"settings": config.settings.model_copy(update={"paths": paths})})


def test_apply_profile_scales_sizing_not_the_floor():
    base = make_config().limits
    agg = rp.apply_profile(base, "aggressive")
    # sizing fields move up
    assert agg.position.risk_per_trade_pct == 2.0
    assert agg.portfolio.max_gross_exposure_pct == 150.0
    assert agg.orders.max_new_trades_per_day == 8
    assert agg.options.max_loss_per_trade_usd == 2000.0
    # safety floor is untouched
    assert agg.loss_kill_switch.max_daily_loss_pct == base.loss_kill_switch.max_daily_loss_pct
    assert agg.options.min_days_to_expiry == base.options.min_days_to_expiry
    assert agg.options.defined_risk_only is True
    assert agg.portfolio.drawdown_circuit_pct == base.portfolio.drawdown_circuit_pct
    # conservative dials the same fields down; balanced is identity
    con = rp.apply_profile(base, "conservative")
    assert con.position.risk_per_trade_pct == 0.5
    assert con.portfolio.max_gross_exposure_pct == 60.0
    assert rp.apply_profile(base, "balanced") is base


def test_aggressive_is_gated_without_track_record(tmp_path):
    config = _config_in(tmp_path)
    journal = Journal(config.settings.paths.journal_db)
    try:
        d = rp.check_eligibility(journal, config.settings.tax, "aggressive")
        assert not d.eligible  # 0 scored trades
        # non-forced set is refused and writes nothing
        refused = rp.set_profile(config, journal, config.settings.tax, "aggressive")
        assert not refused.eligible
        assert rp.read_active(config) == "balanced"
        # conservative/balanced are always available
        assert rp.check_eligibility(journal, config.settings.tax, "conservative").eligible
    finally:
        journal.close()


def test_force_applies_and_journals(tmp_path):
    config = _config_in(tmp_path)
    journal = Journal(config.settings.paths.journal_db)
    try:
        d = rp.set_profile(config, journal, config.settings.tax, "aggressive", force=True)
        assert d.eligible  # forced through
        assert rp.read_active(config) == "aggressive"
        # the override is auditable
        beats = journal.conn.execute(
            "SELECT detail FROM heartbeats WHERE job='risk_profile'").fetchall()
        assert any("FORCED" in (row[0] or "") for row in beats)
    finally:
        journal.close()

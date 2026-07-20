"""Track C integration: the /api/portfolio_risk endpoint adapts live positions and
returns aggregate risk."""

from fastapi.testclient import TestClient

from conftest import make_config
from stubs import StubBroker, make_account

from trading.broker.models import PositionView


def _client(tmp_path, positions):
    config = make_config()
    config.settings.paths.journal_db = str(tmp_path / "j.db")
    account = make_account(equity=100000.0, positions=positions)
    from trading.web.app import create_app
    app = create_app(get_config_fn=lambda: config,
                     broker_factory=lambda c: StubBroker(account))
    return TestClient(app)


def test_portfolio_risk_endpoint(tmp_path):
    pos = PositionView(symbol="AAPL", qty=100, avg_entry_price=100.0,
                       market_value=10000.0, unrealized_pl=0.0, asset_class="stock")
    r = _client(tmp_path, [pos]).get("/api/portfolio_risk")
    assert r.status_code == 200
    body = r.json()
    assert body["available"] is True
    assert body["gross_exposure"] > 0
    assert "summary" in body

"""The request-validation surface, tested without a database.

Everything here is rejected *before* a handler touches Postgres — a malformed DSL, a request
body missing a field — so the app runs against fakes: an injected session factory and queue
that the lifespan therefore never has to build a real connection for.
"""

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tradeforge_api.config import Settings
from tradeforge_api.main import create_app


class _FakeQueue:
    async def enqueue_job(self, *args: object) -> None:
        return None


@pytest.fixture
def client() -> Iterator[TestClient]:
    # Only the queue is faked. The session factory is left to the lifespan: `create_engine` is
    # lazy, so no connection is dialled, and every case here is rejected before a handler ever
    # queries — so no Postgres is needed even though a real (unused) engine is built.
    app = create_app(
        settings=Settings(postgres_password="unused-in-unit-tests"),
        arq_pool=_FakeQueue(),
    )
    with TestClient(app) as test_client:
        yield test_client


def _valid_strategy() -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "name": "MA cross",
        "timeframe": "H1",
        "indicators": [
            {"id": "fast", "type": "SMA", "params": {"period": 2}},
            {"id": "slow", "type": "SMA", "params": {"period": 3}},
        ],
        "entry": {
            "long": {"op": "crosses_above", "left": {"ref": "fast"}, "right": {"ref": "slow"}},
            "short": None,
        },
        "exit": {
            "stop_loss": {"type": "candle_extreme", "params": {"lookback": 2, "side": "low"}},
            "conditions": [],
        },
        "risk": {"sizing": {"type": "percent_risk", "params": {"percent": 1.0}}},
    }


def test_a_malformed_strategy_is_rejected_before_the_database(client: TestClient) -> None:
    """Wrong shape — the DSL's Pydantic layer refuses it, and no session is ever opened."""
    response = client.post("/strategies", json={"not": "a strategy"})
    assert response.status_code == 422
    assert response.json()["detail"]["message"] == "strategy failed schema validation"


def test_a_reference_to_an_undeclared_indicator_is_rejected(client: TestClient) -> None:
    """Right shape, wrong meaning: the entry names an indicator that was never declared. The
    semantic layer catches what the schema cannot."""
    document = _valid_strategy()
    document["entry"]["long"]["left"] = {"ref": "ghost"}
    response = client.post("/strategies", json=document)
    assert response.status_code == 422
    assert response.json()["detail"]["message"] == "strategy is well-formed but cannot run"


def test_a_backtest_request_missing_a_field_is_a_422(client: TestClient) -> None:
    response = client.post("/backtests", json={"symbol": "EURUSD"})
    assert response.status_code == 422


def test_a_backtest_request_with_an_unknown_field_is_a_422(client: TestClient) -> None:
    """`extra="forbid"` — a typo'd field name is an error, not silently dropped."""
    response = client.post(
        "/backtests",
        json={
            "strategy_id": "00000000-0000-0000-0000-000000000000",
            "symbol": "EURUSD",
            "timeframe": "H1",
            "date_from": "2024-01-01T00:00:00Z",
            "date_to": "2024-02-01T00:00:00Z",
            "initial_capital": "10000",
            "typo_field": True,
        },
    )
    assert response.status_code == 422

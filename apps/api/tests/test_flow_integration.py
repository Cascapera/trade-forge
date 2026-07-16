"""The whole flow, against real Postgres: create a strategy, enqueue a backtest, run the
worker, read the results back.

The queue is a capturing fake (so no arq process is needed) and the worker is invoked inline
via `process_backtest` — everything else is real: the HTTP surface, the database, the engine,
the persistence mapper. This is the PR-107 acceptance criterion as one test.

Run locally with:  docker compose up -d  &&  uv run pytest -m integration
"""

import asyncio
import datetime as dt
import uuid
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from tradeforge_api.config import Settings
from tradeforge_api.main import create_app
from tradeforge_api.worker import process_backtest
from tradeforge_collector import write_candles
from tradeforge_db.models import Instrument
from tradeforge_engine.domain import AssetClass, Candle
from tradeforge_engine.testing import bar

pytestmark = pytest.mark.integration

START = dt.datetime(2024, 1, 1, tzinfo=dt.UTC)
HOUR = dt.timedelta(hours=1)


class _CapturingQueue:
    """Stands in for the arq pool: records what would have been enqueued instead of sending it."""

    def __init__(self) -> None:
        self.jobs: list[tuple[str, tuple[Any, ...]]] = []

    async def enqueue_job(self, function: str, *args: Any) -> None:
        self.jobs.append((function, args))


class _RecordingRedis:
    """Stands in for Redis: records the progress the worker publishes."""

    def __init__(self) -> None:
        self.events: list[str] = []

    async def publish(self, channel: str, message: str) -> None:
        self.events.append(message)


def _seed_instrument(session: Session) -> Instrument:
    instrument = Instrument(
        symbol="EURUSD",
        name="Euro vs US Dollar",
        asset_class=AssetClass.FOREX,
        currency_base="EUR",
        currency_quote="USD",
        tick_size=Decimal("0.00001"),
        tick_value=Decimal("1"),
        contract_size=Decimal("100000"),
        digits=5,
    )
    session.add(instrument)
    session.commit()
    return instrument


def _candles() -> list[Candle]:
    levels = [
        "1.10500",
        "1.10400",
        "1.10300",
        "1.10200",
        "1.10300",
        "1.10500",
        "1.10800",
        "1.11200",
        "1.11700",
        "1.12300",
        "1.13000",
        "1.13800",
    ]
    return [bar(i, open_=levels[i], close=levels[i + 1]) for i in range(len(levels) - 1)]


def _strategy() -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "name": "MA cross flow",
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
            "take_profit": {"type": "risk_multiple", "params": {"rr": 2.0}},
            "conditions": [],
        },
        "risk": {"sizing": {"type": "percent_risk", "params": {"percent": 1.0}}},
    }


def test_create_enqueue_run_and_read(
    session_factory: Callable[[], Session], settings: Settings, tmp_path: Path
) -> None:
    seeding = session_factory()
    _seed_instrument(seeding)
    seeding.close()
    write_candles(tmp_path, "EURUSD", "H1", _candles())

    queue = _CapturingQueue()
    app = create_app(
        settings=settings.model_copy(update={"parquet_root": tmp_path}),
        session_factory=session_factory,
        arq_pool=queue,
    )

    with TestClient(app) as client:
        created = client.post("/strategies", json=_strategy())
        assert created.status_code == 201, created.text
        strategy_id = created.json()["id"]

        enqueued = client.post(
            "/backtests",
            json={
                "strategy_id": strategy_id,
                "symbol": "EURUSD",
                "timeframe": "H1",
                "date_from": START.isoformat(),
                "date_to": (START + 100 * HOUR).isoformat(),
                "initial_capital": "10000",
                "cost_model": {"type": "none"},
            },
        )
        assert enqueued.status_code == 202, enqueued.text
        backtest_id = enqueued.json()["id"]
        # The API dropped exactly one job and did not run the engine itself.
        assert queue.jobs == [("run_backtest", (backtest_id,))]
        assert client.get(f"/backtests/{backtest_id}").json()["status"] == "queued"

        # Run the worker inline, the way arq would have.
        redis = _RecordingRedis()
        worker_session = session_factory()
        try:
            asyncio.run(
                process_backtest(
                    session=worker_session,
                    redis=redis,  # type: ignore[arg-type]
                    parquet_root=tmp_path,
                    backtest_id=uuid.UUID(backtest_id),
                )
            )
        finally:
            worker_session.close()

        # The worker announced its progress: running, then done.
        assert any('"running"' in event for event in redis.events)
        assert any('"done"' in event for event in redis.events)

        finished = client.get(f"/backtests/{backtest_id}").json()
        assert finished["status"] == "done"
        assert finished["error"] is None
        assert finished["metrics"] is not None
        assert finished["metrics"]["total_trades"] >= 1

        trades = client.get(f"/backtests/{backtest_id}/trades").json()
        assert trades["total"] == finished["metrics"]["total_trades"]
        assert len(trades["items"]) == trades["total"]
        first = trades["items"][0]
        assert first["direction"] in {"long", "short"}
        assert isinstance(first["net_pnl"], str)  # money is a string on the wire, never a float

        equity = client.get(f"/backtests/{backtest_id}/equity").json()
        assert len(equity) >= 1
        assert isinstance(equity[0]["equity"], str)


def test_a_backtest_for_an_unknown_symbol_is_rejected(
    session_factory: Callable[[], Session], settings: Settings, tmp_path: Path
) -> None:
    seeding = session_factory()
    strategy_row_source = _strategy()
    seeding.close()

    app = create_app(
        settings=settings.model_copy(update={"parquet_root": tmp_path}),
        session_factory=session_factory,
        arq_pool=_CapturingQueue(),
    )
    with TestClient(app) as client:
        strategy_id = client.post("/strategies", json=strategy_row_source).json()["id"]
        response = client.post(
            "/backtests",
            json={
                "strategy_id": strategy_id,
                "symbol": "NOPE",
                "timeframe": "H1",
                "date_from": START.isoformat(),
                "date_to": (START + HOUR).isoformat(),
                "initial_capital": "10000",
            },
        )
        assert response.status_code == 422
        assert "NOPE" in response.json()["detail"]

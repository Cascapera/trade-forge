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
from sqlalchemy import Engine, event
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


def _assert_the_snapshot_is_advertised_then_served(
    client: TestClient, backtest_id: str, strategy_id: str, first: dict[str, Any]
) -> None:
    """The list says a picture exists; a second call fetches it.

    Split out of the flow test because it is a claim of its own — that the cost of the window
    is not paid by every reader of the trades table — and because the flow test is already at
    the statement limit. It runs inside that test rather than beside it: a finished run over a
    real Postgres is expensive to build, and this needs one.
    """
    # Asserted on the serialised body, not on the model: what this buys is bytes on the wire,
    # so a field that quietly came back would restore the cost with nothing failing.
    assert first["has_snapshot"] is True
    assert "snapshot" not in first
    assert "bars" not in client.get(f"/backtests/{backtest_id}/trades").text

    served = client.get(f"/backtests/{backtest_id}/trades/{first['id']}/snapshot")
    assert served.status_code == 200, served.text
    window = served.json()
    assert len(window["bars"]) >= 2
    assert isinstance(window["bars"][0]["close"], str)  # exact decimals, never JSON floats
    # The window ends on the bar that filled: that equality is what pins a chart to its row.
    assert window["filled_at"] == first["entry_time"]
    assert any(bar["time"] == window["decided_at"] for bar in window["bars"])

    # Trade ids are globally unique, so the backtest in the path is not needed to *find* the
    # row — it is there so a wrong run is a 404 instead of another run's chart.
    other = client.post(
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
    ).json()["id"]
    assert client.get(f"/backtests/{other}/trades/{first['id']}/snapshot").status_code == 404
    assert client.get(f"/backtests/{backtest_id}/trades/999999999/snapshot").status_code == 404


def _assert_the_run_log_lists_both_runs_without_their_curves(
    client: TestClient, backtest_id: str, strategy_id: str
) -> None:
    """The list endpoint, checked once two runs exist — one finished, one still queued.

    Called after the snapshot helper because that one leaves a second backtest behind, which is
    what makes the filters and the `total` worth asserting at all: over a single row, every
    filter that matches looks identical to one that does not filter.
    """
    page = client.get("/backtests").json()
    assert page["total"] == 2
    assert [item["id"] for item in page["items"]][:1] != [], "newest first, and non-empty"

    listed = {item["id"]: item for item in page["items"]}
    finished = listed[backtest_id]

    # What this schema adds over `BacktestOut`: the ids resolved into what a human reads.
    assert finished["symbol"] == "EURUSD"
    assert finished["strategy_id"] == strategy_id
    assert finished["strategy_name"] == _strategy()["name"]
    assert finished["strategy_version"] == 1
    assert finished["cost_model"] == {"type": "none"}
    assert finished["metrics"]["total_trades"] >= 1

    # The queued run has no metrics at all — null, never a fabricated row of zeroes, which would
    # read in a comparison table as a strategy that traded and made nothing.
    queued = next(item for item in page["items"] if item["id"] != backtest_id)
    assert queued["status"] == "queued"
    assert queued["metrics"] is None

    # The curve is absent from the list *and* exists for this run: asserting only the absence
    # would pass just as well against a run that never had one.
    assert len(client.get(f"/backtests/{backtest_id}/equity").json()) >= 1
    assert "equity" not in client.get("/backtests").text

    # Filters bite, and the negative case is a symbol that exists in the catalogue rather than a
    # nonsense one, so a filter that silently matched nothing would still look wrong here.
    assert client.get("/backtests", params={"symbol": "EURUSD"}).json()["total"] == 2
    assert client.get("/backtests", params={"symbol": "AAPL"}).json()["total"] == 0
    assert client.get("/backtests", params={"status": "done"}).json()["total"] == 1
    assert client.get("/backtests", params={"timeframe": "H1"}).json()["total"] == 2
    assert client.get("/backtests", params={"timeframe": "M15"}).json()["total"] == 0

    # `total` counts the matching rows, not the page: a client sizing its pager off `len(items)`
    # would stop after the first page and quietly hide every run before it.
    one = client.get("/backtests", params={"limit": 1}).json()
    assert one["total"] == 2
    assert len(one["items"]) == 1
    assert (
        client.get("/backtests", params={"limit": 1, "offset": 1}).json()["items"][0]["id"]
        != (one["items"][0]["id"])
    )


def _assert_listing_costs_the_same_whatever_the_page_holds(
    client: TestClient, engine: Engine
) -> None:
    """The N+1 guard, stated as the property it actually is rather than as a magic number.

    `Backtest.metrics` lazy-loads by default, so a handler that merely touches it emits one extra
    query per row — and each of those drags the run's equity curve out of Postgres, a JSONB
    column measured at up to 856 kB apiece on this project's own database. Neither shows up in
    the response, which is what makes the regression invisible: the payload stays correct and
    only the clock moves. Measured over 36 real runs, lazy took 37 queries and 145 ms against 2
    queries and 4.8 ms with the curve deferred.

    Asserting a query *count* would encode today's implementation and break on any harmless
    refactor. The property that matters is that the count does not depend on how many rows come
    back: eager loading answers a one-row page and a two-row page with the same statements, lazy
    loading needs one more for the second row. So the two pages are compared to each other, and
    nothing is claimed about the absolute number.
    """
    counted: list[int] = []

    def count(*_args: object, **_kwargs: object) -> None:
        counted[-1] += 1

    event.listen(engine, "before_cursor_execute", count)
    try:
        for limit in (1, 2):
            counted.append(0)
            assert client.get("/backtests", params={"limit": limit}).status_code == 200
    finally:
        event.remove(engine, "before_cursor_execute", count)

    one_row, two_rows = counted
    assert one_row > 0, "the listener saw nothing, so this proves nothing"
    assert one_row == two_rows, (
        f"listing one run took {one_row} queries and listing two took {two_rows}: "
        f"the per-row query is back, and with it the curve nobody reads"
    )


def test_create_enqueue_run_and_read(
    session_factory: Callable[[], Session],
    settings: Settings,
    tmp_path: Path,
    migrated_engine: Engine,
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

        # The run says which candles it read, and the row survived the CHECK that keeps the
        # three provenance columns in step. Both only happen against a real Postgres.
        series = _candles()
        assert finished["candles_seen"] == len(series)
        assert finished["first_candle"] == series[0].time.isoformat().replace("+00:00", "Z")
        assert finished["last_candle"] == series[-1].time.isoformat().replace("+00:00", "Z")

        trades = client.get(f"/backtests/{backtest_id}/trades").json()
        assert trades["total"] == finished["metrics"]["total_trades"]
        assert len(trades["items"]) == trades["total"]
        first = trades["items"][0]
        assert first["direction"] in {"long", "short"}
        assert isinstance(first["net_pnl"], str)  # money is a string on the wire, never a float

        _assert_the_snapshot_is_advertised_then_served(client, backtest_id, strategy_id, first)
        _assert_the_run_log_lists_both_runs_without_their_curves(client, backtest_id, strategy_id)
        _assert_listing_costs_the_same_whatever_the_page_holds(client, migrated_engine)

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


def test_a_timeframe_with_no_collected_candles_fails_instead_of_finishing_empty(
    session_factory: Callable[[], Session], settings: Settings, tmp_path: Path
) -> None:
    """The bug this PR exists for, end to end.

    The symbol is catalogued and the strategy is valid — only the *timeframe* has never been
    collected. This used to finish `done` with every metric at zero, which is indistinguishable
    on screen from a strategy that found no setups.
    """
    seeding = session_factory()
    _seed_instrument(seeding)
    seeding.close()
    write_candles(tmp_path, "EURUSD", "H1", _candles())  # H1 exists; the run will ask for M15

    app = create_app(
        settings=settings.model_copy(update={"parquet_root": tmp_path}),
        session_factory=session_factory,
        arq_pool=_CapturingQueue(),
    )

    with TestClient(app) as client:
        strategy_id = client.post("/strategies", json=_strategy()).json()["id"]
        backtest_id = client.post(
            "/backtests",
            json={
                "strategy_id": strategy_id,
                "symbol": "EURUSD",
                "timeframe": "M15",
                "date_from": START.isoformat(),
                "date_to": (START + 100 * HOUR).isoformat(),
                "initial_capital": "10000",
                "cost_model": {"type": "none"},
            },
        ).json()["id"]

        worker_session = session_factory()
        try:
            asyncio.run(
                process_backtest(
                    session=worker_session,
                    redis=_RecordingRedis(),  # type: ignore[arg-type]
                    parquet_root=tmp_path,
                    backtest_id=uuid.UUID(backtest_id),
                )
            )
        finally:
            worker_session.close()

        finished = client.get(f"/backtests/{backtest_id}").json()

        assert finished["status"] == "failed"
        assert "M15" in finished["error"]
        assert finished["metrics"] is None
        # Nothing was read, so nothing is claimed. Null is the honest answer, not zero.
        assert finished["candles_seen"] is None
        assert finished["first_candle"] is None

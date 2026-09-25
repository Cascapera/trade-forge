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
from sqlalchemy import Engine, event, func, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from tradeforge_api import retention
from tradeforge_api.config import Settings
from tradeforge_api.main import create_app
from tradeforge_api.worker import process_backtest
from tradeforge_db.models import Backtest, BacktestMetrics, Instrument, Sweep
from tradeforge_engine.domain import AssetClass, Candle
from tradeforge_engine.testing import bar

pytestmark = pytest.mark.integration

START = dt.datetime(2024, 1, 1, tzinfo=dt.UTC)
HOUR = dt.timedelta(hours=1)


class _CapturingQueue:
    """Stands in for the arq pool: records what would have been enqueued instead of sending it."""

    def __init__(self) -> None:
        self.jobs: list[tuple[str, tuple[Any, ...]]] = []

    async def enqueue_job(self, function: str, *args: Any, **options: Any) -> None:
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

    # An offset past what Postgres can render as a bigint is refused by validation, not handed to
    # the driver. Unbounded, it reached the database and came back as `NumericValueOutOfRange` —
    # a 500 on input a client fully controls, which is what schemathesis caught. The two assertions
    # are a pair on purpose: the first pins the rejection, the second pins that the bound is the
    # type's limit and not something narrower, so a legitimate deep page still answers 200.
    beyond = 2**63
    assert client.get("/backtests", params={"offset": beyond}).status_code == 422
    assert client.get("/backtests", params={"offset": beyond - 1}).status_code == 200

    # The same hole existed on the trades listing since long before this endpoint, and the fuzzer
    # simply happened to draw `/backtests` first. Fixing one and leaving the other would have left
    # the identical 500 one URL away.
    assert (
        client.get(f"/backtests/{backtest_id}/trades", params={"offset": beyond}).status_code == 422
    )
    assert (
        client.get(f"/backtests/{backtest_id}/trades", params={"offset": beyond - 1}).status_code
        == 200
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
    collected: Callable[..., None],
    migrated_engine: Engine,
) -> None:
    seeding = session_factory()
    _seed_instrument(seeding)
    seeding.close()
    collected(tmp_path, "EURUSD", "H1", _candles())

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
        # How far it went, measured by the engine and stored through migration 0022.
        assert first["mfe_r"] is not None
        assert Decimal(first["mfe_r"]) >= 0
        assert Decimal(first["mae_r"]) >= 0
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


def test_a_timeframe_the_index_has_never_seen_is_refused_at_launch(
    session_factory: Callable[[], Session],
    settings: Settings,
    tmp_path: Path,
    collected: Callable[..., None],
) -> None:
    """H1 is collected, M15 never was: the launch says so instead of queueing a run to fail."""
    seeding = session_factory()
    _seed_instrument(seeding)
    seeding.close()
    collected(tmp_path, "EURUSD", "H1", _candles())
    queue = _CapturingQueue()
    app = create_app(
        settings=settings.model_copy(update={"parquet_root": tmp_path}),
        session_factory=session_factory,
        arq_pool=queue,
    )

    with TestClient(app) as client:
        strategy_id = client.post("/strategies", json=_strategy()).json()["id"]
        body = {
            "strategy_id": strategy_id,
            "symbol": "EURUSD",
            "timeframe": "M15",
            "date_from": START.isoformat(),
            "date_to": (START + 100 * HOUR).isoformat(),
            "initial_capital": "10000",
            "cost_model": {"type": "none"},
        }
        refused = client.post("/backtests", json=body)
        listed = client.get("/backtests").json()

    assert refused.status_code == 422
    assert refused.json()["detail"] == "no candles in this window for EURUSD M15 (never collected)"
    assert queue.jobs == []
    assert listed["total"] == 0


def test_a_window_the_data_does_not_reach_is_refused_at_launch(
    session_factory: Callable[[], Session],
    settings: Settings,
    tmp_path: Path,
    collected: Callable[..., None],
) -> None:
    """Collected, but for other dates: the refusal names what the disk does hold."""
    seeding = session_factory()
    _seed_instrument(seeding)
    seeding.close()
    collected(tmp_path, "EURUSD", "H1", _candles())
    with TestClient(
        create_app(
            settings=settings.model_copy(update={"parquet_root": tmp_path}),
            session_factory=session_factory,
            arq_pool=_CapturingQueue(),
        )
    ) as client:
        strategy_id = client.post("/strategies", json=_strategy()).json()["id"]
        refused = client.post(
            "/backtests",
            json={
                "strategy_id": strategy_id,
                "symbol": "EURUSD",
                "timeframe": "H1",
                "date_from": (START + 1000 * HOUR).isoformat(),
                "date_to": (START + 1100 * HOUR).isoformat(),
                "initial_capital": "10000",
                "cost_model": {"type": "none"},
            },
        )

    assert refused.status_code == 422
    day = START.date().isoformat()
    assert refused.json()["detail"] == (
        f"no candles in this window for EURUSD H1 (on disk: {day} to "
        f"{(START + HOUR * (len(_candles()) - 1)).date().isoformat()})"
    )


def _launch_over(
    session_factory: Callable[[], Session],
    settings: Settings,
    tmp_path: Path,
    collected: Callable[..., None],
    **window: str,
) -> Any:
    """EURUSD H1 collected from `START`, then one launch over `window`."""
    seeding = session_factory()
    _seed_instrument(seeding)
    seeding.close()
    collected(tmp_path, "EURUSD", "H1", _candles())
    with TestClient(
        create_app(
            settings=settings.model_copy(update={"parquet_root": tmp_path}),
            session_factory=session_factory,
            arq_pool=_CapturingQueue(),
        )
    ) as client:
        strategy_id = client.post("/strategies", json=_strategy()).json()["id"]
        return client.post(
            "/backtests",
            json={
                "strategy_id": strategy_id,
                "symbol": "EURUSD",
                "timeframe": "H1",
                "initial_capital": "10000",
                "cost_model": {"type": "none"},
                **window,
            },
        )


def test_a_window_that_starts_on_the_last_bar_is_accepted(
    session_factory: Callable[[], Session],
    settings: Settings,
    tmp_path: Path,
    collected: Callable[..., None],
) -> None:
    """⚠️ The worker reads `date_from <= time <= date_to`, both ends closed, so a window opening
    exactly on the last bar reads that bar. A launch that compared with `<=` refused it — a D1
    run from the day of the last daily bar, which is what the screen sends at midnight."""
    last = _candles()[-1].time
    response = _launch_over(
        session_factory,
        settings,
        tmp_path,
        collected,
        date_from=last.isoformat(),
        date_to=(last + 5 * HOUR).isoformat(),
    )
    assert response.status_code == 202, response.text


def test_a_window_that_ends_on_the_first_bar_is_accepted(
    session_factory: Callable[[], Session],
    settings: Settings,
    tmp_path: Path,
    collected: Callable[..., None],
) -> None:
    response = _launch_over(
        session_factory,
        settings,
        tmp_path,
        collected,
        date_from=(START - 5 * HOUR).isoformat(),
        date_to=START.isoformat(),
    )
    assert response.status_code == 202, response.text


@pytest.mark.parametrize("field", ["date_from", "date_to"])
def test_an_instant_without_a_timezone_is_refused_not_a_500(
    session_factory: Callable[[], Session],
    settings: Settings,
    tmp_path: Path,
    collected: Callable[..., None],
    field: str,
) -> None:
    """A naive instant cannot be compared with the index's `timestamptz`; before the launch
    asked the index it was silently accepted, and after, it was a 500."""
    window = {"date_from": START.isoformat(), "date_to": (START + 5 * HOUR).isoformat()}
    window[field] = "2024-01-01T02:00:00"
    response = _launch_over(session_factory, settings, tmp_path, collected, **window)
    assert response.status_code == 422, response.text
    assert "timezone" in response.text


def test_a_timeframe_with_no_collected_candles_fails_instead_of_finishing_empty(
    session_factory: Callable[[], Session],
    settings: Settings,
    tmp_path: Path,
    collected: Callable[..., None],
    indexed: Callable[..., None],
) -> None:
    """The bug this PR exists for, end to end.

    The symbol is catalogued and the strategy is valid — only the *timeframe* has no bars. This
    used to finish `done` with every metric at zero, which is indistinguishable on screen from a
    strategy that found no setups.

    ⚠️ Since PR-262 the launch refuses a timeframe the index has never seen (the test below), so
    this one reaches the worker the only way left: an index that claims M15 while the disk holds
    none. The worker's refusal is the guard for exactly that disagreement.
    """
    seeding = session_factory()
    _seed_instrument(seeding)
    seeding.close()
    collected(tmp_path, "EURUSD", "H1", _candles())  # H1 exists; the run will ask for M15
    indexed("EURUSD", "M15")  # ...and the index wrongly says M15 does too

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


def _launch(client: TestClient) -> str:
    strategy_id = client.post("/strategies", json=_strategy()).json()["id"]
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
    return str(enqueued.json()["id"])


def _work(session_factory: Callable[[], Session], parquet_root: Path, backtest_id: str) -> None:
    session = session_factory()
    try:
        asyncio.run(
            process_backtest(
                session=session,
                redis=_RecordingRedis(),  # type: ignore[arg-type]
                parquet_root=parquet_root,
                backtest_id=uuid.UUID(backtest_id),
            )
        )
    finally:
        session.close()


def _app(
    session_factory: Callable[[], Session],
    settings: Settings,
    tmp_path: Path,
    collected: Callable[..., None],
) -> TestClient:
    seeding = session_factory()
    _seed_instrument(seeding)
    seeding.close()
    collected(tmp_path, "EURUSD", "H1", _candles())
    return TestClient(
        create_app(
            settings=settings.model_copy(update={"parquet_root": tmp_path}),
            session_factory=session_factory,
            arq_pool=_CapturingQueue(),
        )
    )


def test_a_database_lost_mid_run_is_not_recorded_as_the_runs_failure_and_a_retry_finishes_it(
    session_factory: Callable[[], Session],
    settings: Settings,
    tmp_path: Path,
    collected: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The connection drops after the run is marked running; the retry picks it up from there.

    Before the fix, the broad `except` would record the driver's error as the run's result — a
    backtest reported as failed for a reason that says nothing about the strategy.
    """
    with _app(session_factory, settings, tmp_path, collected) as client:
        backtest_id = _launch(client)

        def lost(**_kwargs: object) -> object:
            raise OperationalError("COMMIT", {}, Exception("server closed the connection"))

        with monkeypatch.context() as patched:
            patched.setattr("tradeforge_api.worker.execute_backtest", lost)
            with pytest.raises(OperationalError):
                _work(session_factory, tmp_path, backtest_id)

        interrupted = client.get(f"/backtests/{backtest_id}").json()
        assert interrupted["status"] == "running"
        assert interrupted["error"] is None

        _work(session_factory, tmp_path, backtest_id)

        finished = client.get(f"/backtests/{backtest_id}").json()
        assert finished["status"] == "done"
        assert finished["error"] is None
        assert finished["metrics"] is not None


def test_an_error_the_database_answered_is_recorded_on_the_first_try(
    session_factory: Callable[[], Session],
    settings: Settings,
    tmp_path: Path,
    collected: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cancelled query is an answer, not an absence: the run fails now, with that reason."""

    class _CancelledError(Exception):
        sqlstate = "57014"

    with _app(session_factory, settings, tmp_path, collected) as client:
        backtest_id = _launch(client)

        def cancelled(**_kwargs: object) -> object:
            raise OperationalError(
                "SELECT", {}, _CancelledError("canceling statement due to timeout")
            )

        with monkeypatch.context() as patched:
            patched.setattr("tradeforge_api.worker.execute_backtest", cancelled)
            _work(session_factory, tmp_path, backtest_id)

        failed = client.get(f"/backtests/{backtest_id}").json()
        assert failed["status"] == "failed"
        assert "canceling statement due to timeout" in failed["error"]


def test_a_walk_forward_fold_records_an_unreachable_database_on_the_run(
    session_factory: Callable[[], Session],
    settings: Settings,
    tmp_path: Path,
    collected: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The inline caller cannot retry, so the run is failed rather than left `running`."""
    with _app(session_factory, settings, tmp_path, collected) as client:
        backtest_id = _launch(client)

        def lost(**_kwargs: object) -> object:
            raise OperationalError("COMMIT", {}, Exception("server closed the connection"))

        with monkeypatch.context() as patched:
            patched.setattr("tradeforge_api.worker.execute_backtest", lost)
            session = session_factory()
            try:
                asyncio.run(
                    process_backtest(
                        session=session,
                        redis=_RecordingRedis(),  # type: ignore[arg-type]
                        parquet_root=tmp_path,
                        backtest_id=uuid.UUID(backtest_id),
                        retry_unreachable=False,
                    )
                )
            finally:
                session.close()

        failed = client.get(f"/backtests/{backtest_id}").json()
        assert failed["status"] == "failed"
        assert "server closed the connection" in failed["error"]


def test_a_retry_of_a_failed_run_keeps_the_reason_it_failed(
    session_factory: Callable[[], Session],
    settings: Settings,
    tmp_path: Path,
    collected: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The failure was recorded but its commit's answer was lost; the retry must not restart it.

    Restarting would stamp a start after the recorded finish, and the CHECK refusing that would
    write its own message over the real reason.
    """
    with _app(session_factory, settings, tmp_path, collected) as client:
        backtest_id = _launch(client)

        def broken(**_kwargs: object) -> object:
            raise ValueError("the strategy is wrong")

        with monkeypatch.context() as patched:
            patched.setattr("tradeforge_api.worker.execute_backtest", broken)
            _work(session_factory, tmp_path, backtest_id)
        first = client.get(f"/backtests/{backtest_id}").json()
        assert first["status"] == "failed"

        _work(session_factory, tmp_path, backtest_id)

        again = client.get(f"/backtests/{backtest_id}").json()
        assert again["status"] == "failed"
        assert again["error"] == "the strategy is wrong"
        assert again["finished_at"] == first["finished_at"]


def test_a_retry_of_a_finished_run_leaves_its_result_alone(
    session_factory: Callable[[], Session],
    settings: Settings,
    tmp_path: Path,
    collected: Callable[..., None],
) -> None:
    """A commit whose answer was lost with the connection is retried; the run had already landed.

    Running it again would write a second metrics row against the same key, and the collision
    would be recorded as the failure of a run that had succeeded.
    """
    with _app(session_factory, settings, tmp_path, collected) as client:
        backtest_id = _launch(client)
        _work(session_factory, tmp_path, backtest_id)
        first = client.get(f"/backtests/{backtest_id}").json()
        assert first["status"] == "done"

        _work(session_factory, tmp_path, backtest_id)

        again = client.get(f"/backtests/{backtest_id}").json()
        assert again["status"] == "done"
        assert again["error"] is None
        assert again["finished_at"] == first["finished_at"]
        assert again["metrics"] == first["metrics"]
        reading = session_factory()
        try:
            rows = reading.scalar(
                select(func.count())
                .select_from(BacktestMetrics)
                .where(BacktestMetrics.backtest_id == uuid.UUID(backtest_id))
            )
        finally:
            reading.close()
        assert rows == 1


# --------------------------------------------------------------------------- #
# What a sweep's run keeps (`retention`, 2026-09-23)                           #
# --------------------------------------------------------------------------- #


def _into_a_sweep(session_factory: Callable[[], Session], backtest_id: str) -> None:
    """Make a launched run a sweep's, the way `POST /sweeps` would have written it."""
    session = session_factory()
    try:
        sweep = Sweep(
            entry_ids=[],
            symbols=["EURUSD"],
            timeframes=["H1"],
            date_from=START,
            date_to=START + 100 * HOUR,
            initial_capital=Decimal(10000),
        )
        session.add(sweep)
        session.flush()
        run = session.get(Backtest, uuid.UUID(backtest_id))
        assert run is not None
        run.sweep_id = sweep.id
        session.commit()
    finally:
        session.close()


def test_a_single_run_keeps_everything(
    session_factory: Callable[[], Session],
    settings: Settings,
    tmp_path: Path,
    collected: Callable[..., None],
) -> None:
    with _app(session_factory, settings, tmp_path, collected) as client:
        backtest_id = _launch(client)
        _work(session_factory, tmp_path, backtest_id)

        run = client.get(f"/backtests/{backtest_id}").json()
        assert run["recorded"] == "full"
        trades = client.get(f"/backtests/{backtest_id}/trades").json()["items"]
        assert [trade["has_snapshot"] for trade in trades] == [True]
        assert client.get(f"/backtests/{backtest_id}/equity").status_code == 200

        # Every run scores the target ladder (migration 0023). This document carries a 2 R target
        # of its own, so the rungs up to it are scored and the ones above it cannot be.
        ladder = run["targets"]
        assert ladder is not None
        assert ladder["1"]["trades"] == run["metrics"]["total_trades"]
        assert ladder["2"] is not None
        assert ladder["3"] is None


def test_a_sweeps_run_below_the_floor_keeps_only_its_metrics(
    session_factory: Callable[[], Session],
    settings: Settings,
    tmp_path: Path,
    collected: Callable[..., None],
) -> None:
    """The fixture makes one trade and a profit on H1, where the floor is 30: a profit over one
    trade is not a run worth reading trade by trade, so only the metrics stay — and every number a
    sweep ranks by is still there."""
    with _app(session_factory, settings, tmp_path, collected) as client:
        backtest_id = _launch(client)
        _into_a_sweep(session_factory, backtest_id)
        _work(session_factory, tmp_path, backtest_id)

        run = client.get(f"/backtests/{backtest_id}").json()
        assert run["status"] == "done"
        assert run["recorded"] == "metrics"
        assert run["metrics"]["total_trades"] == 1
        assert Decimal(run["metrics"]["net_profit"]) > 0
        # ⚠️ The risk in R is computed while the trades are in memory (25/09), so a run that keeps
        # none still carries it — the losers are exactly what a selection is judged against. One
        # winning trade: positive R, no fall from a peak, no losing streak, one year and no share.
        metrics = run["metrics"]
        assert Decimal(metrics["net_r"]) > 0
        assert Decimal(metrics["max_drawdown_r"]) == 0
        assert (metrics["losing_streak"], Decimal(metrics["losing_streak_r"])) == (0, 0)
        assert metrics["positive_year_share"] is None
        (only,) = metrics["yearly_r"].values()
        assert Decimal(only) == Decimal(metrics["net_r"])

        assert client.get(f"/backtests/{backtest_id}/trades").json()["items"] == []
        equity = client.get(f"/backtests/{backtest_id}/equity")
        assert equity.status_code == 404
        assert "kept no equity curve" in equity.json()["detail"]


def test_a_sweeps_run_over_the_floor_keeps_its_trades_without_pictures(
    session_factory: Callable[[], Session],
    settings: Settings,
    tmp_path: Path,
    collected: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The same run with H1's floor lowered to one trade: now it passed, and its trades stay —
    without the entry pictures, which the engine did not build, and without the curve."""
    monkeypatch.setitem(retention.MIN_TRADES, "H1", 1)
    with _app(session_factory, settings, tmp_path, collected) as client:
        backtest_id = _launch(client)
        _into_a_sweep(session_factory, backtest_id)
        _work(session_factory, tmp_path, backtest_id)

        assert client.get(f"/backtests/{backtest_id}").json()["recorded"] == "trades"
        trades = client.get(f"/backtests/{backtest_id}/trades").json()["items"]
        assert [trade["has_snapshot"] for trade in trades] == [False]
        assert client.get(f"/backtests/{backtest_id}/equity").status_code == 404


def test_running_a_sweeps_point_again_keeps_everything_and_changes_nothing(
    session_factory: Callable[[], Session],
    settings: Settings,
    tmp_path: Path,
    collected: Callable[..., None],
) -> None:
    """⚠️ The promise the whole cut rests on: what a sweep's run did not keep is rebuilt, exactly,
    by running the point again. A new run in no sweep, so it keeps everything — and its metrics
    are the sweep run's to the last digit, because the engine is deterministic."""
    with _app(session_factory, settings, tmp_path, collected) as client:
        backtest_id = _launch(client)
        _into_a_sweep(session_factory, backtest_id)
        _work(session_factory, tmp_path, backtest_id)

        again = client.post(f"/backtests/{backtest_id}/rerun")
        assert again.status_code == 202, again.text
        again_id = again.json()["id"]
        assert again_id != backtest_id
        _work(session_factory, tmp_path, again_id)

        original = client.get(f"/backtests/{backtest_id}").json()
        rerun = client.get(f"/backtests/{again_id}").json()
        assert rerun["recorded"] == "full"
        assert rerun["metrics"] == original["metrics"]
        assert client.get(f"/backtests/{again_id}/equity").status_code == 200
        trades = client.get(f"/backtests/{again_id}/trades").json()["items"]
        assert [trade["has_snapshot"] for trade in trades] == [True]


def test_a_run_not_yet_finished_cannot_be_run_again(
    session_factory: Callable[[], Session],
    settings: Settings,
    tmp_path: Path,
    collected: Callable[..., None],
) -> None:
    with _app(session_factory, settings, tmp_path, collected) as client:
        backtest_id = _launch(client)
        refused = client.post(f"/backtests/{backtest_id}/rerun")
        assert refused.status_code == 409
        assert "queued" in refused.json()["detail"]

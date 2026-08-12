"""`/baskets` over a real Postgres: what the arithmetic tests deliberately cannot reach.

`test_baskets.py` proves the dispersion arithmetic on in-memory rows, which is the right
place for it. Everything *here* is the part that only exists once there is a database and an
HTTP layer: that three symbols become three rows and three jobs in one transaction, that each
row is charged its own instrument's measured spread, that a basket naming one bad symbol
writes nothing at all, and that deleting the grouping does not delete the measurements.

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
from sqlalchemy import Engine, event, select
from sqlalchemy.orm import Session

from tradeforge_api.config import Settings
from tradeforge_api.main import create_app
from tradeforge_api.worker import process_backtest
from tradeforge_collector import write_candles
from tradeforge_db.models import Backtest, Basket, Instrument
from tradeforge_engine.domain import AssetClass, Candle
from tradeforge_engine.testing import bar

pytestmark = pytest.mark.integration

START = dt.datetime(2024, 1, 1, tzinfo=dt.UTC)
HOUR = dt.timedelta(hours=1)
CAPITAL = "10000"

# The three catalogued symbols and the spread each was measured at, in ticks. `US500` is NULL
# on purpose and mirrors this project's own database, where nobody has ever measured it.
SPREADS: dict[str, str | None] = {"EURUSD": "8", "GBPUSD": "9", "US500": None}


class _CapturingQueue:
    """Stands in for the arq pool: records what would have been enqueued instead of sending it."""

    def __init__(self) -> None:
        self.jobs: list[tuple[str, tuple[Any, ...]]] = []

    async def enqueue_job(self, function: str, *args: Any) -> None:
        self.jobs.append((function, args))


class _SilentRedis:
    """Stands in for Redis while the worker runs inline. Progress is not this file's subject."""

    async def publish(self, channel: str, message: str) -> None:
        return None


def _seed_instruments(session: Session) -> None:
    """Three catalogued symbols, two with a measured spread and one without."""
    for symbol, spread in SPREADS.items():
        session.add(
            Instrument(
                symbol=symbol,
                name=f"{symbol} for the basket tests",
                asset_class=AssetClass.FOREX if symbol != "US500" else AssetClass.INDEX,
                currency_base="EUR" if symbol != "US500" else None,
                currency_quote="USD",
                tick_size=Decimal("0.00001"),
                tick_value=Decimal("1"),
                contract_size=Decimal("100000"),
                digits=5,
                default_spread_points=None if spread is None else Decimal(spread),
            )
        )
    session.commit()


_LEVELS = [
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


def _series(levels: list[str]) -> list[Candle]:
    """Consecutive closes into consecutive hourly bars, starting at `START`."""
    return [bar(i, open_=levels[i], close=levels[i + 1]) for i in range(len(levels) - 1)]


def _rising() -> list[Candle]:
    """A series the MA cross trades: the fast average crosses up and the move continues."""
    return _series(_LEVELS)


def _falling() -> list[Candle]:
    """The same levels walked backwards: a market that only falls, so the long never fires.

    A basket exists to show a strategy behaving differently in different markets, and "never
    triggered here" is one of the outcomes it has to be able to report. It also keeps the
    read-back test from being vacuous — two identical series would produce two identical
    returns, and an aggregate that named the extremes backwards would look just as correct.

    ⚠️ The **levels** are reversed and the bars are then built forwards. Reversing the finished
    candles would carry their timestamps along with them, handing the loader a series that runs
    from the last hour to the first — which is not a falling market, it is a corrupt one.
    """
    return _series(list(reversed(_LEVELS)))


def _strategy() -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "name": "MA cross across markets",
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


def _basket_body(strategy_id: str, symbols: list[str], **overrides: Any) -> dict[str, Any]:
    return {
        "strategy_id": strategy_id,
        "symbols": symbols,
        "timeframe": "H1",
        "date_from": START.isoformat(),
        "date_to": (START + 100 * HOUR).isoformat(),
        "initial_capital": CAPITAL,
        **overrides,
    }


def _app(
    settings: Settings, session_factory: Callable[[], Session], tmp_path: Path, queue: Any
) -> Any:
    return create_app(
        settings=settings.model_copy(update={"parquet_root": tmp_path}),
        session_factory=session_factory,
        arq_pool=queue,
    )


def _run_worker(session_factory: Callable[[], Session], tmp_path: Path, backtest_id: str) -> None:
    """Invoke the worker inline, the way arq would have."""
    session = session_factory()
    try:
        asyncio.run(
            process_backtest(
                session=session,
                redis=_SilentRedis(),  # type: ignore[arg-type]
                parquet_root=tmp_path,
                backtest_id=uuid.UUID(backtest_id),
            )
        )
    finally:
        session.close()


def test_each_market_in_a_basket_is_charged_its_own_measured_spread(
    session_factory: Callable[[], Session], settings: Settings, tmp_path: Path
) -> None:
    """The reason a basket takes no `cost_model`: there is no single cost across markets.

    Three symbols go out with three different cost models, resolved by the server from the
    catalogue. The two measured ones carry their own tick counts — 8 and 9 are not
    interchangeable, and a basket that charged one figure to all three would be comparing
    markets on a cost that is wrong for at least two of them.

    ⚠️ The symbol nobody measured runs **uncosted**, and the assertion that it is not
    `spread_points: 0` is the point of this test rather than a detail of it: zero is the claim
    that US500 is free to trade, and it would flatter that run against the two paying markets
    by exactly the amount nobody knows.
    """
    seeding = session_factory()
    _seed_instruments(seeding)
    seeding.close()

    queue = _CapturingQueue()
    with TestClient(_app(settings, session_factory, tmp_path, queue)) as client:
        strategy_id = client.post("/strategies", json=_strategy()).json()["id"]

        # Deliberately not alphabetical: the response has to come back in the order asked.
        symbols = ["US500", "EURUSD", "GBPUSD"]
        created = client.post("/baskets", json=_basket_body(strategy_id, symbols))
        assert created.status_code == 202, created.text
        body = created.json()

        assert [run["symbol"] for run in body["runs"]] == symbols
        charged = {run["symbol"]: run for run in body["runs"]}

        # Compared as `Decimal`, not as text. Postgres renders a NUMERIC at its column scale,
        # so the wire carries "8.0000000000"; asserting that literal would nail this test to
        # the number of decimal places the price column happens to declare, and widening the
        # column would then break a test about *costs* for no reason a reader could follow.
        assert charged["EURUSD"]["cost_model"]["type"] == "spread"
        assert Decimal(charged["EURUSD"]["cost_model"]["spread_points"]) == Decimal("8")
        assert charged["GBPUSD"]["cost_model"]["type"] == "spread"
        assert Decimal(charged["GBPUSD"]["cost_model"]["spread_points"]) == Decimal("9")

        # ⚠️ The uncosted symbol, and this is the assertion the test exists for: `type: none`,
        # never `spread_points: 0`. Zero is the claim that US500 is free to trade, and it would
        # flatter that run against the two paying markets by exactly the amount nobody knows.
        assert charged["US500"]["cost_model"] == {"type": "none"}
        assert "spread_points" not in charged["US500"]["cost_model"]

        # Money on the wire is a string, never a JSON float: the spread is a Decimal in the
        # catalogue and a Decimal in the engine, and this row is the document that reproduces
        # the run. A number here would be the one place an exact tick count went binary.
        assert isinstance(charged["EURUSD"]["cost_model"]["spread_points"], str)

        # Surfaced per row, not as a basket-wide footnote — it is true of one symbol and not
        # the others, and the reader is the one who has to know which.
        assert charged["US500"]["default_spread_points"] is None
        assert Decimal(charged["EURUSD"]["default_spread_points"]) == Decimal("8")

        assert all(run["status"] == "queued" for run in body["runs"])

        # One job per symbol, and the ids are the runs that were just created — a basket that
        # wrote three rows and enqueued two would sit half-finished forever.
        assert queue.jobs == [("run_backtest", (run["backtest_id"],)) for run in body["runs"]]

    verifying = session_factory()
    try:
        stored = verifying.scalars(select(Backtest)).all()
        assert len(stored) == len(symbols)
        assert {str(run.basket_id) for run in stored} == {body["id"]}
    finally:
        verifying.close()


def test_a_basket_reads_back_as_the_spread_of_outcomes_across_its_markets(
    session_factory: Callable[[], Session], settings: Settings, tmp_path: Path
) -> None:
    """Two markets, both finished, and the aggregate agreeing with the rows it summarises.

    The expected numbers are **derived from the runs' own metrics** rather than written down.
    Hard-coding what the engine returns would turn this into a test of the engine, which has
    its own; what is being checked here is that the basket reports the runs it actually
    grouped, resolved through real foreign keys.
    """
    seeding = session_factory()
    _seed_instruments(seeding)
    seeding.close()
    write_candles(tmp_path, "EURUSD", "H1", _rising())
    write_candles(tmp_path, "GBPUSD", "H1", _falling())

    with TestClient(_app(settings, session_factory, tmp_path, _CapturingQueue())) as client:
        strategy_id = client.post("/strategies", json=_strategy()).json()["id"]
        created = client.post(
            "/baskets", json=_basket_body(strategy_id, ["EURUSD", "GBPUSD"])
        ).json()

        for run in created["runs"]:
            _run_worker(session_factory, tmp_path, run["backtest_id"])

        read = client.get(f"/baskets/{created['id']}")
        assert read.status_code == 200, read.text
        basket = read.json()

        # How it was launched, with the strategy's foreign key resolved into what a human reads.
        assert basket["strategy_id"] == strategy_id
        assert basket["strategy_name"] == _strategy()["name"]
        assert basket["strategy_version"] == 1
        assert basket["timeframe"] == "H1"
        assert Decimal(basket["initial_capital"]) == Decimal(CAPITAL)

        # Ordered by symbol, so the same basket reads the same way twice.
        assert [run["symbol"] for run in basket["runs"]] == ["EURUSD", "GBPUSD"]
        assert all(run["status"] == "done" for run in basket["runs"])

        returns = {
            run["symbol"]: Decimal(run["metrics"]["net_profit"]) / Decimal(CAPITAL)
            for run in basket["runs"]
        }
        best, worst = max(returns, key=returns.__getitem__), min(returns, key=returns.__getitem__)
        assert returns[best] != returns[worst], (
            "both markets returned the same thing, so this test cannot tell the extremes apart"
        )

        aggregate = basket["aggregate"]
        assert aggregate["runs_total"] == 2
        assert aggregate["runs_finished"] == 2
        assert aggregate["runs_failed"] == 0
        assert aggregate["best_symbol"] == best
        assert aggregate["worst_symbol"] == worst
        assert Decimal(aggregate["best_return"]) == returns[best]
        assert Decimal(aggregate["worst_return"]) == returns[worst]
        assert Decimal(aggregate["median_return"]) == (returns[best] + returns[worst]) / 2

        # ⚠️ No summed curve, and its absence is the design: two runs of $10 000 are neither a
        # $10 000 account nor a $20 000 one. Asserted on the serialised body because a field
        # that quietly came back would draw a portfolio line that was never measured.
        assert "equity" not in read.text


def test_reading_a_basket_costs_the_same_however_many_runs_it_holds(
    session_factory: Callable[[], Session],
    settings: Settings,
    tmp_path: Path,
    migrated_engine: Engine,
) -> None:
    """The N+1 guard, stated as the property it is rather than as a magic number.

    `Backtest.metrics` lazy-loads by default and `list_item` touches it, so without the
    `selectinload` the handler emits one extra query per run — and each of those drags the
    run's equity curve, a JSONB column measured at up to 856 kB apiece on this project's own
    database, out of Postgres to build a row that does not contain it. The payload stays
    correct and only the clock moves, which is what makes the regression invisible.

    So the counts of a two-run basket and a three-run basket are compared to each other and
    nothing is claimed about the absolute number, which would encode today's implementation.
    """
    seeding = session_factory()
    _seed_instruments(seeding)
    seeding.close()

    with TestClient(_app(settings, session_factory, tmp_path, _CapturingQueue())) as client:
        strategy_id = client.post("/strategies", json=_strategy()).json()["id"]
        two = client.post("/baskets", json=_basket_body(strategy_id, ["EURUSD", "GBPUSD"])).json()
        three = client.post(
            "/baskets", json=_basket_body(strategy_id, ["EURUSD", "GBPUSD", "US500"])
        ).json()

        counted: list[int] = []

        def count(*_args: object, **_kwargs: object) -> None:
            counted[-1] += 1

        event.listen(migrated_engine, "before_cursor_execute", count)
        try:
            for basket in (two, three):
                counted.append(0)
                assert client.get(f"/baskets/{basket['id']}").status_code == 200
        finally:
            event.remove(migrated_engine, "before_cursor_execute", count)

    two_runs, three_runs = counted
    assert two_runs > 0, "the listener saw nothing, so this proves nothing"
    assert two_runs == three_runs, (
        f"a two-run basket took {two_runs} queries and a three-run basket took {three_runs}: "
        f"the per-row query is back, and with it the curve nobody reads"
    )


def test_one_unknown_symbol_refuses_the_whole_basket_and_names_every_bad_one(
    session_factory: Callable[[], Session], settings: Settings, tmp_path: Path
) -> None:
    """All or nothing, and the error is a list rather than the first offender.

    A basket that half-exists is worse than one that was refused: the caller asked a single
    question about four markets, and two runs plus an error answers a question nobody asked.
    The two assertions on the database are what make that claim; a 422 alone is consistent
    with rows having been written before the bad symbol was reached.

    Both bad symbols are named because a caller fixing a typo list one round trip at a time
    is a caller the API is failing.
    """
    seeding = session_factory()
    _seed_instruments(seeding)
    seeding.close()

    queue = _CapturingQueue()
    with TestClient(_app(settings, session_factory, tmp_path, queue)) as client:
        strategy_id = client.post("/strategies", json=_strategy()).json()["id"]
        refused = client.post(
            "/baskets", json=_basket_body(strategy_id, ["EURUSD", "NOPE", "GBPUSD", "ALSONOPE"])
        )

        assert refused.status_code == 422
        detail = refused.json()["detail"]
        assert "NOPE" in detail
        assert "ALSONOPE" in detail
        assert queue.jobs == []

    verifying = session_factory()
    try:
        assert verifying.scalars(select(Basket)).all() == []
        assert verifying.scalars(select(Backtest)).all() == []
    finally:
        verifying.close()


def test_a_basket_that_is_not_a_comparison_is_refused(
    session_factory: Callable[[], Session], settings: Settings, tmp_path: Path
) -> None:
    """The three ways a request fails before any symbol is looked up.

    A repeated symbol is refused rather than de-duplicated: silently returning three runs for
    four requested symbols leaves the caller's own list disagreeing with the basket. A basket
    of one is a backtest, and `POST /backtests` is better at it. And a ceiling exists for the
    same reason every numeric query parameter needs one — without it a single POST enqueues
    as many jobs as the caller likes.
    """
    seeding = session_factory()
    _seed_instruments(seeding)
    seeding.close()

    with TestClient(_app(settings, session_factory, tmp_path, _CapturingQueue())) as client:
        strategy_id = client.post("/strategies", json=_strategy()).json()["id"]

        repeated = client.post(
            "/baskets", json=_basket_body(strategy_id, ["EURUSD", "GBPUSD", "EURUSD"])
        )
        assert repeated.status_code == 422
        assert "EURUSD" in repeated.text

        alone = client.post("/baskets", json=_basket_body(strategy_id, ["EURUSD"]))
        assert alone.status_code == 422

        crowd = [f"SYM{i}" for i in range(21)]
        assert client.post("/baskets", json=_basket_body(strategy_id, crowd)).status_code == 422


def test_a_basket_is_refused_when_the_launch_itself_makes_no_sense(
    session_factory: Callable[[], Session], settings: Settings, tmp_path: Path
) -> None:
    """An unknown strategy, an unknown timeframe, and a window that runs backwards."""
    seeding = session_factory()
    _seed_instruments(seeding)
    seeding.close()

    with TestClient(_app(settings, session_factory, tmp_path, _CapturingQueue())) as client:
        strategy_id = client.post("/strategies", json=_strategy()).json()["id"]
        pair = ["EURUSD", "GBPUSD"]

        missing = client.post("/baskets", json=_basket_body(str(uuid.uuid4()), pair))
        assert missing.status_code == 404

        bad_timeframe = client.post(
            "/baskets", json=_basket_body(strategy_id, pair, timeframe="H3")
        )
        assert bad_timeframe.status_code == 422

        backwards = client.post(
            "/baskets",
            json=_basket_body(
                strategy_id, pair, date_from=(START + HOUR).isoformat(), date_to=START.isoformat()
            ),
        )
        assert backwards.status_code == 422


def test_deleting_a_basket_leaves_the_runs_it_grouped_standing(
    session_factory: Callable[[], Session], settings: Settings, tmp_path: Path
) -> None:
    """`ON DELETE SET NULL`, which is the difference between forgetting and destroying.

    "I no longer care about this comparison" is not "destroy three backtests and their
    trades". Only a real Postgres can make this claim: the rule lives in the foreign key, and
    the relationship is mapped `passive_deletes="all"` precisely so the database applies it
    instead of SQLAlchemy loading every run to NULL its column one at a time.
    """
    seeding = session_factory()
    _seed_instruments(seeding)
    seeding.close()

    with TestClient(_app(settings, session_factory, tmp_path, _CapturingQueue())) as client:
        strategy_id = client.post("/strategies", json=_strategy()).json()["id"]
        created = client.post(
            "/baskets", json=_basket_body(strategy_id, ["EURUSD", "GBPUSD", "US500"])
        ).json()
        run_ids = {run["backtest_id"] for run in created["runs"]}

        deleting = session_factory()
        try:
            deleting.delete(deleting.get(Basket, uuid.UUID(created["id"])))
            deleting.commit()
        finally:
            deleting.close()

        assert client.get(f"/baskets/{created['id']}").status_code == 404
        # Every run still answers, and still carries its own cost model and window.
        for run_id in run_ids:
            standing = client.get(f"/backtests/{run_id}")
            assert standing.status_code == 200, standing.text
            assert standing.json()["status"] == "queued"

    verifying = session_factory()
    try:
        assert verifying.scalars(select(Basket)).all() == []
        orphaned = verifying.scalars(select(Backtest)).all()
        assert {str(run.id) for run in orphaned} == run_ids
        assert all(run.basket_id is None for run in orphaned)
    finally:
        verifying.close()


def test_an_unknown_basket_is_a_404_rather_than_an_empty_one(
    session_factory: Callable[[], Session], settings: Settings, tmp_path: Path
) -> None:
    """An empty aggregate would read as "this comparison ran and found nothing"."""
    with TestClient(_app(settings, session_factory, tmp_path, _CapturingQueue())) as client:
        assert client.get(f"/baskets/{uuid.uuid4()}").status_code == 404

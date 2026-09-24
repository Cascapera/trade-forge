"""A run that was told to collect first, and the worker that waits for it.

His rule (17/09): answered "collect", the run should happen by itself once the download lands.
What only a database can prove is the part that makes that work: the collections and the run are
written together and linked, and the worker reads that link rather than guessing from the dates.

⚠️ No MetaTrader and no arq process anywhere. The collection is moved by hand, exactly as the host
agent would have moved it, and `run_backtest` is called the way arq calls it.
"""

import asyncio
import datetime as dt
import uuid
from collections.abc import Callable, Iterator
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from tradeforge_api.candle_cache import CandleCache
from tradeforge_api.config import Settings
from tradeforge_api.main import create_app
from tradeforge_api.queue import COLLECT_QUEUE, COLLECT_RANGE, RUN_BACKTEST
from tradeforge_api.worker import WAIT_POLL_SECONDS, run_backtest
from tradeforge_db.broker_symbols import BrokerSymbolEntry, replace_snapshot
from tradeforge_db.models import (
    Backtest,
    BacktestCollection,
    BacktestStatus,
    Collection,
    Dataset,
    Instrument,
)
from tradeforge_engine.domain import AssetClass
from tradeforge_engine.testing import bar

pytestmark = pytest.mark.integration

START = dt.datetime(2024, 1, 1, tzinfo=dt.UTC)

# A short H1 series, enough for a run that is about waiting rather than about trading.
CANDLES = [bar(i, open_="1.10000", close="1.10100") for i in range(50)]


class _Queue:
    """Records what would have been enqueued, on which queue and how long deferred."""

    def __init__(self) -> None:
        self.jobs: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    async def enqueue_job(self, function: str, *args: Any, **options: Any) -> None:
        self.jobs.append((function, args, options))

    async def publish(self, channel: str, message: str) -> None:
        return None

    def of(self, function: str) -> list[dict[str, Any]]:
        return [options for name, _args, options in self.jobs if name == function]


@pytest.fixture
def queue() -> _Queue:
    return _Queue()


@pytest.fixture
def client(session_factory: Callable[[], Session], queue: _Queue, tmp_path: Any) -> Iterator[Any]:
    seeding = session_factory()
    seeding.add(
        Instrument(
            symbol="EURUSD",
            name="Euro / US Dollar",
            asset_class=AssetClass.FOREX,
            currency_base="EUR",
            currency_quote="USD",
            tick_size=Decimal("0.00001"),
            tick_value=Decimal(1),
            contract_size=Decimal(100000),
            digits=5,
        )
    )
    seeding.commit()
    seeding.close()
    app: Any = create_app(
        settings=Settings().model_copy(update={"parquet_root": tmp_path}),
        session_factory=session_factory,
        arq_pool=queue,
    )
    with TestClient(app) as opened:
        yield opened


def a_strategy(client: Any) -> str:
    document = {
        "schema_version": "1.0",
        "name": f"waiting {uuid.uuid4()}",
        "timeframe": "H1",
        "indicators": [{"id": "fast", "type": "SMA", "params": {"period": 2}}],
        "entry": {
            "long": {"op": "crosses_above", "left": {"ref": "fast"}, "right": {"ref": "fast"}},
            "short": None,
        },
        "exit": {
            "stop_loss": {"type": "candle_extreme", "params": {"lookback": 2, "side": "low"}},
            "take_profit": {"type": "risk_multiple", "params": {"rr": 2.0}},
            "conditions": [],
        },
        "risk": {"sizing": {"type": "percent_risk", "params": {"percent": 1.0}}},
    }
    return str(client.post("/strategies", json=document).json()["id"])


def launch(client: Any, **over: Any) -> Any:
    body = {
        "strategy_id": a_strategy(client),
        "symbol": "EURUSD",
        "timeframe": "H1",
        "date_from": START.isoformat(),
        "date_to": (START + dt.timedelta(days=200)).isoformat(),
        "initial_capital": "10000",
        "cost_model": {"type": "none"},
        **over,
    }
    return client.post("/backtests", json=body)


def waits_of(session_factory: Callable[[], Session], run_id: str) -> list[Collection]:
    with session_factory() as session:
        return list(
            session.scalars(
                select(Collection)
                .join(BacktestCollection, BacktestCollection.collection_id == Collection.id)
                .where(BacktestCollection.backtest_id == uuid.UUID(run_id))
            )
        )


def move(
    session_factory: Callable[[], Session],
    collection: Collection,
    status: BacktestStatus,
    **over: Any,
) -> None:
    """Finish a collection the way the host agent would have."""
    with session_factory() as session:
        row = session.get(Collection, collection.id)
        assert row is not None
        row.status = status
        for field, value in over.items():
            setattr(row, field, value)
        session.commit()


def work(session_factory: Callable[[], Session], queue: _Queue, run_id: str, tmp_path: Any) -> None:
    """Call the job the way arq calls it."""
    ctx: dict[str, Any] = {
        "session_factory": session_factory,
        "redis": queue,
        "settings": Settings().model_copy(update={"parquet_root": tmp_path}),
        "job_try": 1,
        "candles": CandleCache(),
    }
    asyncio.run(run_backtest(ctx, run_id))


def broker_lists(session_factory: Callable[[], Session], *symbols: str) -> None:
    """The host agent's snapshot of the broker's symbols, naming only these."""
    with session_factory() as session:
        replace_snapshot(
            session,
            [BrokerSymbolEntry(symbol=symbol) for symbol in symbols],
            server="Tradeview-Demo",
            synced_at=dt.datetime(2026, 9, 10, tzinfo=dt.UTC),
        )
        session.commit()


class TestTheLaunch:
    def test_without_the_flag_a_window_with_no_candles_is_still_refused(self, client: Any) -> None:
        """The answer "no" keeps PR-262's behaviour: nothing is collected, nothing is queued."""
        refused = launch(client)
        assert refused.status_code == 422
        assert "never collected" in refused.json()["detail"]

    def test_with_the_flag_the_collection_and_the_run_are_written_and_linked(
        self, client: Any, session_factory: Callable[[], Session], queue: _Queue
    ) -> None:
        created = launch(client, collect_missing=True)

        assert created.status_code == 202, created.text
        run_id = created.json()["id"]
        (collection,) = waits_of(session_factory, run_id)
        assert (collection.symbol, collection.timeframe) == ("EURUSD", "H1")
        # Whole calendar years, the plan's rule — not the run's own dates.
        assert (collection.date_from, collection.date_to.year) == (
            dt.datetime(2024, 1, 1, tzinfo=dt.UTC),
            2024,
        )
        assert collection.asset_class is AssetClass.FOREX
        # The collection goes on the host's queue, the run on the worker's.
        assert queue.of(COLLECT_RANGE) == [{"_queue_name": COLLECT_QUEUE}]
        assert len(queue.of(RUN_BACKTEST)) == 1

    def test_with_the_flag_a_symbol_the_broker_does_not_list_is_not_collected(
        self, client: Any, session_factory: Callable[[], Session], queue: _Queue
    ) -> None:
        """⚠️ AAPL and US500 on 18/09: catalogue seeds this broker does not have. Collecting them
        asked MetaTrader for bars of a symbol it does not know and failed; now the launch knows
        before, and refuses as it would any window with nothing to fetch."""
        broker_lists(session_factory, "GBPUSD")

        refused = launch(client, collect_missing=True)

        assert refused.status_code == 422
        assert "never collected" in refused.json()["detail"]
        with session_factory() as session:
            assert session.scalars(select(Collection)).all() == []
        assert queue.of(COLLECT_RANGE) == []

    def test_with_the_flag_and_nothing_missing_it_is_an_ordinary_launch(
        self,
        client: Any,
        session_factory: Callable[[], Session],
        queue: _Queue,
        collected: Any,
        tmp_path: Any,
    ) -> None:
        collected(tmp_path, "EURUSD", "H1", CANDLES)
        created = launch(
            client,
            collect_missing=True,
            date_from=CANDLES[0].time.isoformat(),
            date_to=CANDLES[-1].time.isoformat(),
        )

        assert created.status_code == 202, created.text
        assert waits_of(session_factory, created.json()["id"]) == []
        assert queue.of(COLLECT_RANGE) == []

    def test_a_window_with_nothing_to_collect_and_nothing_to_read_is_still_refused(
        self, client: Any, queue: _Queue
    ) -> None:
        """⚠️ The hole the flag opened. A window wholly in the future has no candles and nothing
        worth downloading, so the plan is empty — and an empty plan must not read as "covered".
        Queued, the run would spend a worker to learn what the index already knew (PR-262)."""
        ahead = dt.datetime.now(tz=dt.UTC) + dt.timedelta(days=30)
        refused = launch(
            client,
            collect_missing=True,
            date_from=ahead.isoformat(),
            date_to=(ahead + dt.timedelta(days=10)).isoformat(),
        )

        assert refused.status_code == 422
        assert "never collected" in refused.json()["detail"]
        assert queue.jobs == []

    def test_a_window_missing_on_both_sides_becomes_two_linked_collections(
        self, client: Any, session_factory: Callable[[], Session], queue: _Queue, indexed: Any
    ) -> None:
        """⚠️ Why this is a table and not a column. The data sits in the middle of the window
        with a clear year either side, so the plan has two gaps — adjacent years would have been
        merged into one window (PR-261) — and the run must wait for **both**."""
        with session_factory() as session:
            instrument = session.scalars(
                select(Instrument).where(Instrument.symbol == "EURUSD")
            ).one()
            session.add(
                Dataset(
                    instrument_id=instrument.id,
                    timeframe="H1",
                    date_from=dt.datetime(2020, 6, 1, tzinfo=dt.UTC),
                    date_to=dt.datetime(2022, 6, 1, tzinfo=dt.UTC),
                    candle_count=1000,
                    parquet_path="EURUSD/H1",
                )
            )
            session.commit()

        created = launch(
            client,
            collect_missing=True,
            date_from=dt.datetime(2018, 1, 1, tzinfo=dt.UTC).isoformat(),
            date_to=dt.datetime(2024, 6, 1, tzinfo=dt.UTC).isoformat(),
        )

        assert created.status_code == 202, created.text
        waits = waits_of(session_factory, created.json()["id"])
        assert len(waits) == 2
        assert len(queue.of(COLLECT_RANGE)) == 2

        # ⚠️ Oldest window first, asserted of the body rather than sorted here: the order is the
        # relationship's (`order_by`), and a test that sorted would pass with it deleted.
        served = client.get(f"/backtests/{created.json()['id']}").json()["waiting_for"]
        assert [(one["date_from"][:4], one["date_to"][:4]) for one in served] == [
            ("2018", "2020"),
            ("2022", "2024"),
        ]

    def test_the_run_says_what_it_is_waiting_for_and_how_far_along_it_is(
        self, client: Any, session_factory: Callable[[], Session]
    ) -> None:
        """⚠️ Waiting looks exactly like queueing from outside — the status is `queued` either
        way. The screen can only tell the difference if the run says what it is waiting for."""
        run_id = launch(
            client,
            collect_missing=True,
            date_from=dt.datetime(2023, 3, 1, tzinfo=dt.UTC).isoformat(),
            date_to=dt.datetime(2024, 7, 1, tzinfo=dt.UTC).isoformat(),
        ).json()["id"]
        (collection,) = waits_of(session_factory, run_id)
        # Two calendar years to fetch, one of them downloaded — the progress the screen shows.
        move(session_factory, collection, BacktestStatus.RUNNING, years_done=1)

        body = client.get(f"/backtests/{run_id}").json()

        assert body["status"] == "queued"
        (waiting,) = body["waiting_for"]
        assert (waiting["symbol"], waiting["timeframe"]) == ("EURUSD", "H1")
        assert (waiting["status"], waiting["years_done"], waiting["years_total"]) == (
            "running",
            1,
            2,
        )

    def test_a_run_waiting_for_nothing_says_so_with_an_empty_list(
        self, client: Any, collected: Any, tmp_path: Any
    ) -> None:
        collected(tmp_path, "EURUSD", "H1", CANDLES)
        created = launch(
            client,
            date_from=CANDLES[0].time.isoformat(),
            date_to=CANDLES[-1].time.isoformat(),
        )
        assert client.get(f"/backtests/{created.json()['id']}").json()["waiting_for"] == []

    def test_the_waits_are_served_oldest_window_first(
        self, client: Any, session_factory: Callable[[], Session]
    ) -> None:
        """⚠️ Written in the reverse order on purpose. A launch creates its collections oldest
        first, so the rows come back sorted whether or not anything sorts them — and a test built
        that way passes with the ordering deleted."""
        run_id = launch(client, collect_missing=True).json()["id"]
        with session_factory() as session:
            for year in (2026, 2019):
                older = Collection(
                    symbol="EURUSD",
                    timeframe="H1",
                    date_from=dt.datetime(year, 1, 1, tzinfo=dt.UTC),
                    date_to=dt.datetime(year, 12, 31, tzinfo=dt.UTC),
                    status=BacktestStatus.QUEUED,
                    years_total=1,
                )
                session.add(older)
                session.flush()
                session.add(
                    BacktestCollection(backtest_id=uuid.UUID(run_id), collection_id=older.id)
                )
                session.commit()

        served = client.get(f"/backtests/{run_id}").json()["waiting_for"]

        assert [one["date_from"][:4] for one in served] == ["2019", "2024", "2026"]


class TestTheWait:
    @pytest.fixture
    def waiting(self, client: Any, session_factory: Callable[[], Session]) -> str:
        created = launch(client, collect_missing=True)
        assert created.status_code == 202, created.text
        return str(created.json()["id"])

    def run_status(self, session_factory: Callable[[], Session], run_id: str) -> Backtest:
        with session_factory() as session:
            row = session.get(Backtest, uuid.UUID(run_id))
            assert row is not None
            return row

    def test_an_unfinished_collection_defers_the_run_instead_of_failing_it(
        self, waiting: str, session_factory: Callable[[], Session], queue: _Queue, tmp_path: Any
    ) -> None:
        queue.jobs.clear()
        work(session_factory, queue, waiting, tmp_path)

        # Re-enqueued rather than retried: an arq retry would spend the budget that exists for a
        # database that is not answering.
        # ⚠️ Thirty seconds written out, not `WAIT_POLL_SECONDS`: asserting the constant against
        # itself passes for any value it is ever given, which a mutant proved on 17/09.
        assert queue.of(RUN_BACKTEST) == [{"_defer_by": dt.timedelta(seconds=30)}]
        assert WAIT_POLL_SECONDS == 30  # and the constant is what the worker used
        assert self.run_status(session_factory, waiting).status is BacktestStatus.QUEUED

    def test_a_collection_already_downloading_defers_the_run_too(
        self, waiting: str, session_factory: Callable[[], Session], queue: _Queue, tmp_path: Any
    ) -> None:
        """⚠️ `running` is not `done`. A worker that waited only for `queued` would start on a
        window whose download is halfway through — the very failure the wait exists for."""
        (collection,) = waits_of(session_factory, waiting)
        move(session_factory, collection, BacktestStatus.RUNNING)
        queue.jobs.clear()
        work(session_factory, queue, waiting, tmp_path)

        assert queue.of(RUN_BACKTEST) == [{"_defer_by": dt.timedelta(seconds=30)}]
        assert self.run_status(session_factory, waiting).status is BacktestStatus.QUEUED

    def test_one_failed_collection_of_two_still_waits_for_the_other(
        self, waiting: str, session_factory: Callable[[], Session], queue: _Queue, tmp_path: Any
    ) -> None:
        """His rule (22/09): a failed download ends *its* wait, not the run's. The sibling still
        downloading is data the run will read, so the run goes on waiting for it — starting now
        would throw away a download that is about to land."""
        (collection,) = waits_of(session_factory, waiting)
        with session_factory() as session:
            second = Collection(
                symbol="EURUSD",
                timeframe="H1",
                date_from=dt.datetime(2019, 1, 1, tzinfo=dt.UTC),
                date_to=dt.datetime(2019, 12, 31, tzinfo=dt.UTC),
                status=BacktestStatus.QUEUED,
                years_total=1,
            )
            session.add(second)
            session.flush()
            session.add(BacktestCollection(backtest_id=uuid.UUID(waiting), collection_id=second.id))
            session.commit()
        move(session_factory, collection, BacktestStatus.FAILED, error="the terminal said no")
        queue.jobs.clear()
        work(session_factory, queue, waiting, tmp_path)

        assert queue.of(RUN_BACKTEST) == [{"_defer_by": dt.timedelta(seconds=30)}]
        assert self.run_status(session_factory, waiting).status is BacktestStatus.QUEUED

    def test_a_failed_collection_lets_the_run_go_ahead_on_what_is_on_disk(
        self,
        waiting: str,
        session_factory: Callable[[], Session],
        queue: _Queue,
        collected: Any,
        tmp_path: Any,
    ) -> None:
        """His rule (22/09): the run happens anyway, over the bars that are there, and says which
        download failed. The bars here are the first fifty hours of a two-hundred-day window —
        a shorter measurement, which the run's own coverage already reports beside the one asked
        for. What used to happen was a failed run and not a single metric."""
        collected(tmp_path, "EURUSD", "H1", CANDLES)
        (collection,) = waits_of(session_factory, waiting)
        move(session_factory, collection, BacktestStatus.FAILED, error="the terminal said no")
        queue.jobs.clear()
        work(session_factory, queue, waiting, tmp_path)

        run = self.run_status(session_factory, waiting)
        assert run.status is BacktestStatus.DONE
        assert run.error is None
        assert queue.of(RUN_BACKTEST) == []
        # And the link that says so stays: the failed download, with its reason, is what every
        # screen reads the warning from (`waiting_for`, `failed_collections`).
        [wait] = waits_of(session_factory, waiting)
        assert (wait.status, wait.error) == (BacktestStatus.FAILED, "the terminal said no")

    def test_a_failed_collection_over_an_empty_disk_fails_on_the_data(
        self, waiting: str, session_factory: Callable[[], Session], queue: _Queue, tmp_path: Any
    ) -> None:
        """Nothing on disk and nothing coming: the run still fails, but on the data — "no candles"
        is the true reason, and the download's failure is on the body beside it."""
        (collection,) = waits_of(session_factory, waiting)
        move(session_factory, collection, BacktestStatus.FAILED, error="the terminal said no")
        queue.jobs.clear()
        work(session_factory, queue, waiting, tmp_path)

        run = self.run_status(session_factory, waiting)
        assert run.status is BacktestStatus.FAILED
        assert run.error is not None
        assert "the terminal said no" not in run.error
        assert queue.of(RUN_BACKTEST) == []

    def test_a_finished_collection_lets_the_run_go_ahead(
        self, waiting: str, session_factory: Callable[[], Session], queue: _Queue, tmp_path: Any
    ) -> None:
        (collection,) = waits_of(session_factory, waiting)
        move(session_factory, collection, BacktestStatus.DONE)
        queue.jobs.clear()
        work(session_factory, queue, waiting, tmp_path)

        # The download brought nothing this test could write to disk, so the run fails on the
        # data — which is the honest answer, and not a wait.
        run = self.run_status(session_factory, waiting)
        assert run.status is BacktestStatus.FAILED
        assert queue.of(RUN_BACKTEST) == []

    def test_a_run_nobody_is_collecting_for_is_not_waited_on(
        self,
        client: Any,
        session_factory: Callable[[], Session],
        queue: _Queue,
        collected: Any,
        tmp_path: Any,
    ) -> None:
        collected(tmp_path, "EURUSD", "H1", CANDLES)
        created = launch(
            client,
            date_from=CANDLES[0].time.isoformat(),
            date_to=CANDLES[-1].time.isoformat(),
        )
        queue.jobs.clear()
        work(session_factory, queue, str(created.json()["id"]), tmp_path)

        assert queue.of(RUN_BACKTEST) == []
        assert (
            self.run_status(session_factory, str(created.json()["id"])).status
            is BacktestStatus.DONE
        )

    def test_a_queue_that_is_still_delivering_keeps_the_wait_alive(
        self, waiting: str, session_factory: Callable[[], Session], queue: _Queue, tmp_path: Any
    ) -> None:
        """⚠️ The case a basket makes ordinary: the host agent downloads one collection at a
        time, so the tenth market's download has not started three hours in — and the run was
        created when the basket was launched. Giving up on it would fail a healthy system."""
        old = dt.datetime.now(tz=dt.UTC) - dt.timedelta(hours=3)
        with session_factory() as session:
            run = session.get(Backtest, uuid.UUID(waiting))
            assert run is not None
            run.created_at = old
            # Somebody else's download landed a minute ago: the queue is moving.
            session.add(
                Collection(
                    symbol="GBPUSD",
                    timeframe="H1",
                    date_from=old,
                    date_to=old,
                    status=BacktestStatus.DONE,
                    years_total=1,
                    years_done=1,
                    finished_at=dt.datetime.now(tz=dt.UTC) - dt.timedelta(minutes=1),
                )
            )
            session.commit()
        queue.jobs.clear()
        work(session_factory, queue, waiting, tmp_path)

        assert queue.of(RUN_BACKTEST) == [{"_defer_by": dt.timedelta(seconds=30)}]
        assert self.run_status(session_factory, waiting).status is BacktestStatus.QUEUED

    def test_a_fresh_run_waits_even_when_the_last_delivery_is_old(
        self, waiting: str, session_factory: Callable[[], Session], queue: _Queue, tmp_path: Any
    ) -> None:
        """⚠️ The clock is the later of the two: this run was created a minute ago, and the last
        thing the queue delivered landed three hours ago — probably because nobody asked for
        anything in between. Timing the delivery alone would fail a run that has barely waited."""
        with session_factory() as session:
            session.add(
                Collection(
                    symbol="GBPUSD",
                    timeframe="H1",
                    date_from=START,
                    date_to=START,
                    status=BacktestStatus.DONE,
                    years_total=1,
                    years_done=1,
                    finished_at=dt.datetime.now(tz=dt.UTC) - dt.timedelta(hours=3),
                )
            )
            session.commit()
        queue.jobs.clear()
        work(session_factory, queue, waiting, tmp_path)

        assert queue.of(RUN_BACKTEST) == [{"_defer_by": dt.timedelta(seconds=30)}]
        assert self.run_status(session_factory, waiting).status is BacktestStatus.QUEUED

    def test_a_wait_that_outlives_the_limit_fails_rather_than_waiting_for_ever(
        self, waiting: str, session_factory: Callable[[], Session], queue: _Queue, tmp_path: Any
    ) -> None:
        # ⚠️ Nothing has landed anywhere since: the queue itself is dead, which is the case
        # this limit exists for — an agent that stopped, a terminal nobody logged in to.
        with session_factory() as session:
            run = session.get(Backtest, uuid.UUID(waiting))
            assert run is not None
            run.created_at = dt.datetime.now(tz=dt.UTC) - dt.timedelta(days=1)
            session.commit()
        queue.jobs.clear()
        work(session_factory, queue, waiting, tmp_path)

        run_row = self.run_status(session_factory, waiting)
        assert run_row.status is BacktestStatus.FAILED
        assert run_row.error is not None
        assert "nothing has been collected" in run_row.error
        assert queue.of(RUN_BACKTEST) == []

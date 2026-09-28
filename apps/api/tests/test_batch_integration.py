"""A sweep's runs over one market run as a batch, and write what they would have written alone.

ADR-0029 through the API and the worker: the launch cuts its runs into batch jobs
(`batching`), and `process_batch` runs each batch with the market read once per bar. The claim
held here is the one that matters to a sweep's reader: every run of a batch is written exactly
as `process_backtest` writes the same run on its own — its metrics, its R, its ladder, what it
read, what it kept.
"""

import asyncio
import datetime as dt
import random
import uuid
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from tradeforge_api.batching import BATCH_SIZE, Batcher, batch_key
from tradeforge_api.candle_cache import CandleCache
from tradeforge_api.config import Settings
from tradeforge_api.main import create_app
from tradeforge_api.queue import RUN_BACKTEST, RUN_BACKTEST_BATCH
from tradeforge_api.warm_window import WarmUp
from tradeforge_api.worker import process_backtest, process_batch
from tradeforge_collector import write_candles
from tradeforge_db.models import (
    Backtest,
    BacktestMetrics,
    BacktestStatus,
    Instrument,
    Recorded,
    Sweep,
)
from tradeforge_engine.domain import AssetClass, Candle

from .collector_fakes import running

pytestmark = pytest.mark.integration

START = dt.datetime(2024, 1, 1, tzinfo=dt.UTC)
HOUR = dt.timedelta(hours=1)
BARS = 1400
_NO_WARMUP = WarmUp(bars=0, span=None)


class _Queue:
    def __init__(self) -> None:
        self.jobs: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    async def enqueue_job(self, function: str, *args: Any, **options: Any) -> None:
        self.jobs.append((function, args, options))

    async def publish(self, channel: str, message: str) -> None:
        return None


def _walk(seed: int) -> list[Candle]:
    """Hourly bars that wander and trend, with an impulse now and then — the gaps his regions
    need. The engine's own walk (`test_market_reading._walk`), bar for bar, so seed 0 trades."""
    rng = random.Random(seed)  # noqa: S311 — a reproducible walk, not a secret
    price = Decimal("1.10000")
    drift = Decimal(rng.randint(-3, 3)) / 100_000
    impulse, push, candles = 0, 0, []
    for index in range(BARS):
        if impulse == 0 and rng.random() < 0.04:
            impulse, push = rng.randint(3, 5), rng.choice([-1, 1]) * rng.randint(80, 160)
        move = push if impulse else rng.randint(-60, 60)
        impulse = max(0, impulse - 1)
        wick = rng.randint(0, 8) if impulse else rng.randint(0, 25)
        open_ = price
        close = max(Decimal("0.50000"), open_ + drift + Decimal(move) / 100_000)
        high = max(open_, close) + Decimal(wick) / 100_000
        low = min(open_, close) - Decimal(rng.randint(0, 25)) / 100_000
        candles.append(
            Candle(
                time=START + index * HOUR,
                open=open_,
                high=high,
                low=low,
                close=close,
                tick_volume=100 + rng.randint(0, 900),
            )
        )
        price = close
    return candles


@pytest.fixture
def queue() -> _Queue:
    return _Queue()


@pytest.fixture
def client(
    session_factory: Callable[[], Session],
    settings: Settings,
    tmp_path: Path,
    queue: _Queue,
    collected: Any,
) -> Any:
    with session_factory() as seeding:
        seeding.add(
            Instrument(
                symbol="EURUSD",
                name="EURUSD for the batch tests",
                asset_class=AssetClass.FOREX,
                currency_base="EUR",
                currency_quote="USD",
                tick_size=Decimal("0.00001"),
                tick_value=Decimal("1"),
                contract_size=Decimal("100000"),
                digits=5,
                default_spread_points=Decimal("8"),
            )
        )
        seeding.commit()
    collected(tmp_path, "EURUSD", "H1", _walk(0))
    app = create_app(
        settings=settings.model_copy(update={"parquet_root": tmp_path, "tradeforge_workers": 1}),
        session_factory=session_factory,
        arq_pool=queue,
        collector=running(),
    )
    with TestClient(app) as opened:
        yield opened


def _entry(client: Any) -> str:
    document = {
        "schema_version": "1.0",
        "name": f"choch {uuid.uuid4()}",
        "timeframe": "H1",
        "setup": {
            "type": "structure_choch",
            "params": {
                "htf": "H4",
                "htf_offset": 0,
                "entry_point": "edge",
                "side": "both",
                "breakeven_at_r": 2.0,
            },
        },
        "exit": {"take_profit": {"type": "risk_multiple", "params": {"rr": 2}}},
        "risk": {"sizing": {"type": "percent_risk", "params": {"percent": 1.0}}},
    }
    strategy = client.post("/strategies", json=document)
    assert strategy.status_code == 201, strategy.text
    entry = client.post(
        "/catalog",
        json={
            "name": f"batched {uuid.uuid4()}",
            "strategy_id": strategy.json()["id"],
            "grid": {
                "setup.params.entry_point": ["edge", "midpoint", "return_pass"],
                "setup.params.side": ["both", "long", "short"],
                "setup.params.breakeven_at_r": [None, 1],
            },
        },
    )
    assert entry.status_code == 201, entry.text
    return str(entry.json()["id"])


def _launch(client: Any, entry: str) -> str:
    launched = client.post(
        "/sweeps",
        json={
            "entry_ids": [entry],
            "symbols": ["EURUSD"],
            "timeframes": ["H1"],
            "date_from": START.isoformat(),
            "date_to": (START + (BARS - 1) * HOUR).isoformat(),
            "initial_capital": "10000",
            "cost_model": {"type": "none"},
            "collect_missing": False,
        },
    )
    assert launched.status_code == 202, launched.text
    return str(launched.json()["id"])


def _written(session_factory: Callable[[], Session], sweep_id: str) -> dict[uuid.UUID, Any]:
    """Each run's written result, by the strategy it ran — the same across two identical sweeps."""
    with session_factory() as session:
        rows = session.execute(
            select(Backtest, BacktestMetrics)
            .join(BacktestMetrics, BacktestMetrics.backtest_id == Backtest.id)
            .where(Backtest.sweep_id == uuid.UUID(sweep_id))
        ).all()
        return {
            run.strategy_id: (
                run.status,
                run.recorded,
                run.candles_seen,
                run.first_candle,
                run.last_candle,
                metrics.total_trades,
                metrics.net_profit,
                metrics.net_r,
                metrics.max_drawdown_r,
                metrics.targets,
                metrics.yearly_r,
            )
            for run, metrics in rows
        }


def test_a_batch_writes_every_run_as_the_run_writes_itself_alone(
    client: Any, session_factory: Callable[[], Session], queue: _Queue, tmp_path: Path
) -> None:
    entry = _entry(client)
    batched = _launch(client, entry)
    jobs = [job for job in queue.jobs if job[0] == RUN_BACKTEST_BATCH]
    assert jobs, "the sweep's runs over one market were not batched"
    assert all(len(job[1][0]) <= BATCH_SIZE for job in jobs)
    cache = CandleCache()
    for _name, args, _options in jobs:
        with session_factory() as session:
            asyncio.run(
                process_batch(
                    session=session,
                    redis=queue,  # type: ignore[arg-type]
                    parquet_root=tmp_path,
                    run_ids=[uuid.UUID(one) for one in args[0]],
                    read=cache.read,
                    # Both sweeps must run: a copy of the batch would prove nothing (`reuse`).
                    reuse=False,
                )
            )

    queue.jobs.clear()
    alone = _launch(client, entry)
    for job in queue.jobs:
        ids = [job[1][0]] if job[0] == RUN_BACKTEST else job[1][0]
        for run_id in ids:
            with session_factory() as session:
                asyncio.run(
                    process_backtest(
                        session=session,
                        redis=queue,  # type: ignore[arg-type]
                        parquet_root=tmp_path,
                        backtest_id=uuid.UUID(run_id),
                        read=cache.read,
                        reuse=False,
                    )
                )

    in_batch = _written(session_factory, batched)
    on_its_own = _written(session_factory, alone)
    assert len(in_batch) == 18
    assert in_batch == on_its_own
    assert all(row[0] is BacktestStatus.DONE for row in in_batch.values())
    # The walk has to have traded, or the equality is between two rows of zeros.
    assert sum(row[5] for row in in_batch.values()) > 0


def test_a_batch_skips_runs_already_finished_and_runs_the_rest(
    client: Any, session_factory: Callable[[], Session], queue: _Queue, tmp_path: Path
) -> None:
    """A batch handed back after its worker died picks up where it was."""
    _launch(client, _entry(client))
    (_name, args, _options) = next(job for job in queue.jobs if job[0] == RUN_BACKTEST_BATCH)
    ids = [uuid.UUID(one) for one in args[0]]
    with session_factory() as session:
        finished = session.get(Backtest, ids[0])
        assert finished is not None
        finished.status = BacktestStatus.FAILED
        finished.error = "finished before the batch came back"
        finished.started_at = finished.finished_at = START
        session.commit()
    with session_factory() as session:
        asyncio.run(
            process_batch(
                session=session,
                redis=queue,  # type: ignore[arg-type]
                parquet_root=tmp_path,
                run_ids=ids,
                read=CandleCache().read,
            )
        )
    with session_factory() as session:
        statuses = [session.get(Backtest, one).status for one in ids]  # type: ignore[union-attr]
        kept = session.get(Backtest, ids[0])
        assert kept is not None
        assert kept.error == "finished before the batch came back"
    assert statuses[0] is BacktestStatus.FAILED
    assert set(statuses[1:]) == {BacktestStatus.DONE}


def test_runs_of_different_markets_handed_as_one_batch_run_each_on_its_own(
    client: Any, session_factory: Callable[[], Session], queue: _Queue, tmp_path: Path
) -> None:
    """A job that does not hold one market is not guessed at: each run goes the slow, sure way."""
    _launch(client, _entry(client))
    batches = [job[1][0] for job in queue.jobs if job[0] == RUN_BACKTEST_BATCH]
    ids = [uuid.UUID(one) for one in batches[0][:3]]
    with session_factory() as session:
        moved = session.get(Backtest, ids[1])
        assert moved is not None
        moved.date_to = moved.date_to - 100 * HOUR  # another window: another market to read
        session.commit()
    with session_factory() as session:
        asyncio.run(
            process_batch(
                session=session,
                redis=queue,  # type: ignore[arg-type]
                parquet_root=tmp_path,
                run_ids=ids,
                read=CandleCache().read,
            )
        )
    with session_factory() as session:
        runs = [session.get(Backtest, one) for one in ids]
    assert all(run is not None and run.status is BacktestStatus.DONE for run in runs)
    assert runs[1] is not None
    assert runs[1].last_candle is not None
    assert runs[1].last_candle <= START + (BARS - 101) * HOUR


class TestTheLaunchCutsBatches:
    def test_a_setup_that_reads_no_shared_market_goes_alone(self) -> None:
        mme9 = {"setup": {"type": "mme9_breakout", "params": {"side": "long", "period": 9}}}
        assert batch_key(mme9, "EURUSD", "H1") is None
        assert batch_key({"entry": {}}, "EURUSD", "H1") is None

    def test_runs_are_cut_by_market_in_batches_of_at_most_the_size(self) -> None:
        batcher = Batcher(size=2)
        eur, gbp = (
            ("EURUSD", "H1", (None, dt.timedelta(0)), _NO_WARMUP),
            ("GBPUSD", "H1", (None, dt.timedelta(0)), _NO_WARMUP),
        )
        ids = [uuid.uuid4() for _ in range(6)]
        for run_id, key in zip(ids, [eur, gbp, eur, None, eur, gbp], strict=True):
            batcher.add(run_id, key)
        assert batcher.jobs() == [
            [ids[0], ids[2]],
            [ids[4]],
            [ids[1], ids[5]],
            [ids[3]],
        ]

    def test_an_m1_batch_is_smaller(self) -> None:
        """M1 has five times M5's bars: a full batch would near the job's timeout on the Xeon."""
        batcher = Batcher()
        ids = [uuid.uuid4() for _ in range(13)]
        for run_id in ids:
            batcher.add(run_id, ("EURUSD", "M1", (None, dt.timedelta(0)), _NO_WARMUP))
        assert [len(job) for job in batcher.jobs()] == [6, 6, 1]

    def test_switched_off_every_run_goes_alone(self) -> None:
        """While a machine's workers predate the batch job, a batch would be dropped by them."""
        batcher = Batcher(enabled=False)
        ids = [uuid.uuid4() for _ in range(3)]
        for run_id in ids:
            batcher.add(run_id, ("EURUSD", "H1", (None, dt.timedelta(0)), _NO_WARMUP))
        assert batcher.jobs() == [[one] for one in ids]


def test_a_launch_with_batching_switched_off_queues_every_run_alone(
    session_factory: Callable[[], Session],
    settings: Settings,
    tmp_path: Path,
    collected: Any,
) -> None:
    queue = _Queue()
    with session_factory() as seeding:
        seeding.add(
            Instrument(
                symbol="EURUSD",
                name="EURUSD for the batch tests",
                asset_class=AssetClass.FOREX,
                currency_base="EUR",
                currency_quote="USD",
                tick_size=Decimal("0.00001"),
                tick_value=Decimal("1"),
                contract_size=Decimal("100000"),
                digits=5,
                default_spread_points=Decimal("8"),
            )
        )
        seeding.commit()
    collected(tmp_path, "EURUSD", "H1", _walk(0))
    app = create_app(
        settings=settings.model_copy(
            update={"parquet_root": tmp_path, "tradeforge_workers": 1, "tradeforge_batch": False}
        ),
        session_factory=session_factory,
        arq_pool=queue,
        collector=running(),
    )
    with TestClient(app) as client:
        _launch(client, _entry(client))
    names = {job[0] for job in queue.jobs}
    assert names == {RUN_BACKTEST}
    assert len(queue.jobs) == 18


def _run_all(
    queue: _Queue,
    session_factory: Callable[[], Session],
    tmp_path: Path,
    *,
    reuse: bool = True,
) -> None:
    """Every run job queued so far, through the worker — batches as batches, the rest alone."""
    cache = CandleCache()
    for name, args, _options in queue.jobs:
        with session_factory() as session:
            if name == RUN_BACKTEST_BATCH:
                asyncio.run(
                    process_batch(
                        session=session,
                        redis=queue,  # type: ignore[arg-type]
                        parquet_root=tmp_path,
                        run_ids=[uuid.UUID(one) for one in args[0]],
                        read=cache.read,
                        reuse=reuse,
                    )
                )
            elif name == RUN_BACKTEST:
                asyncio.run(
                    process_backtest(
                        session=session,
                        redis=queue,  # type: ignore[arg-type]
                        parquet_root=tmp_path,
                        backtest_id=uuid.UUID(args[0]),
                        read=cache.read,
                        reuse=reuse,
                    )
                )
    queue.jobs.clear()


def _origins(session_factory: Callable[[], Session], sweep_id: str) -> dict[uuid.UUID, Any]:
    """Each run's `reused_from`, by strategy."""
    with session_factory() as session:
        return {
            run.strategy_id: run.reused_from
            for run in session.scalars(
                select(Backtest).where(Backtest.sweep_id == uuid.UUID(sweep_id))
            )
        }


def _reserve(session_factory: Callable[[], Session], sweep_id: str) -> None:
    """Make a launched sweep a reserved-window test, which keeps every run's trades."""
    with session_factory() as session:
        sweep = session.get(Sweep, uuid.UUID(sweep_id))
        assert sweep is not None
        sweep.holdout_rule = {"metric": "net_profit", "top_n": 1, "min_trades": {}}
        session.commit()


def _recorded(session_factory: Callable[[], Session], sweep_id: str) -> dict[uuid.UUID, Any]:
    with session_factory() as session:
        return {
            run.strategy_id: run.recorded
            for run in session.scalars(
                select(Backtest).where(Backtest.sweep_id == uuid.UUID(sweep_id))
            )
        }


class TestTheRunWarmsUpBeforeItsWindow:
    """ADR-0030 through the API and the worker: a sweep whose window opens in the middle of the
    data reads the bars before it to warm up, records how many, and books from `date_from`."""

    OPENS = 700

    def launch_later(self, client: Any, entry: str) -> str:
        launched = client.post(
            "/sweeps",
            json={
                "entry_ids": [entry],
                "symbols": ["EURUSD"],
                "timeframes": ["H1"],
                "date_from": (START + self.OPENS * HOUR).isoformat(),
                "date_to": (START + (BARS - 1) * HOUR).isoformat(),
                "initial_capital": "10000",
                "cost_model": {"type": "none"},
                "collect_missing": False,
            },
        )
        assert launched.status_code == 202, launched.text
        return str(launched.json()["id"])

    def test_a_batch_warms_up_and_books_as_each_run_alone(
        self, client: Any, session_factory: Callable[[], Session], queue: _Queue, tmp_path: Path
    ) -> None:
        entry = _entry(client)
        batched = self.launch_later(client, entry)
        assert any(job[0] == RUN_BACKTEST_BATCH for job in queue.jobs)
        _run_all(queue, session_factory, tmp_path, reuse=False)
        alone = self.launch_later(client, entry)
        cache = CandleCache()
        for name, args, _options in queue.jobs:
            ids = [args[0]] if name == RUN_BACKTEST else args[0]
            for run_id in ids:
                with session_factory() as session:
                    asyncio.run(
                        process_backtest(
                            session=session,
                            redis=queue,  # type: ignore[arg-type]
                            parquet_root=tmp_path,
                            backtest_id=uuid.UUID(run_id),
                            read=cache.read,
                            reuse=False,
                        )
                    )
        queue.jobs.clear()

        assert _written(session_factory, batched) == _written(session_factory, alone)
        with session_factory() as session:
            runs = list(
                session.scalars(select(Backtest).where(Backtest.sweep_id == uuid.UUID(batched)))
            )
            # A structure setup warms a year; the history holds the 700 bars before the window.
            assert {run.warmup_bars for run in runs} == {self.OPENS}
            assert {run.first_candle for run in runs} == {START + self.OPENS * HOUR}
            traded = 0
            for run in runs:
                assert run.metrics is not None
                cells = run.metrics.r_by_years or {}
                total = sum(Decimal(r) for exits in cells.values() for r in exits.values())
                assert total == (run.metrics.net_r or 0)
                traded += run.metrics.total_trades
            assert traded > 0, "the warmed window has to trade, or the checks are vacuous"


class TestTheSameMeasurementIsCopied:
    """28/09, his ask: a run already made under this engine is taken, not run again (`reuse`).

    The walk trades too little on H1 for a sweep's run to pass its floor of 30 and keep its trades,
    so the tests that need trades kept make the sweep a reserved-window test, which keeps them all.
    """

    def test_a_second_identical_sweep_copies_every_run_and_reads_the_same(
        self, client: Any, session_factory: Callable[[], Session], queue: _Queue, tmp_path: Path
    ) -> None:
        entry = _entry(client)
        first = _launch(client, entry)
        _reserve(session_factory, first)
        _run_all(queue, session_factory, tmp_path)
        second = _launch(client, entry)
        _reserve(session_factory, second)
        _run_all(queue, session_factory, tmp_path)

        assert _written(session_factory, second) == _written(session_factory, first)
        with session_factory() as session:
            originals = {
                run.strategy_id: run.id
                for run in session.scalars(
                    select(Backtest).where(Backtest.sweep_id == uuid.UUID(first))
                )
            }
            copies = list(
                session.scalars(select(Backtest).where(Backtest.sweep_id == uuid.UUID(second)))
            )
            assert {run.strategy_id: run.reused_from for run in copies} == originals
            # The API says so, on the run and on the sweep's list of runs (28/09).
            one = copies[0]
            read = client.get(f"/backtests/{one.id}").json()
            assert read["reused_from"] == str(one.reused_from)
            listed = client.get(f"/sweeps/{second}").json()["runs"]
            assert {row["run"]["reused_from"] for row in listed} == {
                str(origin) for origin in originals.values()
            }
            copied = 0
            for copy in copies:
                source = session.get(Backtest, copy.reused_from)
                assert source is not None
                # The trades the copy keeps are the original's, one for one, without pictures.
                assert [(t.entry_time, t.net_pnl, t.context) for t in copy.trades] == [
                    (t.entry_time, t.net_pnl, t.context) for t in source.trades
                ]
                assert all(t.snapshot is None for t in copy.trades)
                assert copy.metrics is not None
                assert copy.metrics.equity_curve is None
                copied += len(copy.trades)
            assert copied > 0, "the walk has to trade, or the copy compares nothing"
        assert set(_origins(session_factory, first).values()) == {None}

    def test_an_original_that_kept_trades_stands_for_a_copy_that_keeps_less(
        self, client: Any, session_factory: Callable[[], Session], queue: _Queue, tmp_path: Path
    ) -> None:
        entry = _entry(client)
        first = _launch(client, entry)
        _reserve(session_factory, first)
        _run_all(queue, session_factory, tmp_path)
        second = _launch(client, entry)
        _run_all(queue, session_factory, tmp_path)

        assert None not in set(_origins(session_factory, second).values())
        # What a sweep's run keeps, as if it had run: under its floor, the metrics alone.
        assert set(_recorded(session_factory, second).values()) == {Recorded.METRICS}
        with session_factory() as session:
            for run in session.scalars(
                select(Backtest).where(Backtest.sweep_id == uuid.UUID(second))
            ):
                assert run.trades == []

    def test_a_copy_that_must_keep_trades_runs_when_the_original_kept_only_metrics(
        self, client: Any, session_factory: Callable[[], Session], queue: _Queue, tmp_path: Path
    ) -> None:
        entry = _entry(client)
        first = _launch(client, entry)
        _run_all(queue, session_factory, tmp_path)
        second = _launch(client, entry)
        _reserve(session_factory, second)
        _run_all(queue, session_factory, tmp_path)

        assert set(_recorded(session_factory, first).values()) == {Recorded.METRICS}
        assert set(_origins(session_factory, second).values()) == {None}
        assert set(_recorded(session_factory, second).values()) == {Recorded.TRADES}

    def test_switched_off_every_run_runs(
        self, client: Any, session_factory: Callable[[], Session], queue: _Queue, tmp_path: Path
    ) -> None:
        entry = _entry(client)
        first = _launch(client, entry)
        _run_all(queue, session_factory, tmp_path)
        second = _launch(client, entry)
        _run_all(queue, session_factory, tmp_path, reuse=False)

        assert set(_origins(session_factory, second).values()) == {None}
        assert _written(session_factory, second) == _written(session_factory, first)

    def test_a_run_over_other_candles_is_never_an_original(
        self, client: Any, session_factory: Callable[[], Session], queue: _Queue, tmp_path: Path
    ) -> None:
        """The engine-guardian's case (28/09): the same window asked over data collected again.
        A gap in the middle, filled later, keeps the first and last bar — only the count moves."""
        walk = _walk(0)
        write_candles(tmp_path, "EURUSD", "H1", walk[:600] + walk[700:])
        entry = _entry(client)
        first = _launch(client, entry)
        _reserve(session_factory, first)
        _run_all(queue, session_factory, tmp_path)
        write_candles(tmp_path, "EURUSD", "H1", walk)
        second = _launch(client, entry)
        _reserve(session_factory, second)
        _run_all(queue, session_factory, tmp_path)

        assert set(_origins(session_factory, second).values()) == {None}
        with session_factory() as session:
            seen = {
                run.sweep_id: (run.candles_seen, run.first_candle, run.last_candle)
                for run in session.scalars(
                    select(Backtest).where(
                        Backtest.sweep_id.in_([uuid.UUID(first), uuid.UUID(second)])
                    )
                )
            }
        assert seen[uuid.UUID(first)][0] == BARS - 100
        assert seen[uuid.UUID(second)][0] == BARS
        assert seen[uuid.UUID(first)][1:] == seen[uuid.UUID(second)][1:]

    def test_a_run_under_another_engine_is_never_an_original(
        self, client: Any, session_factory: Callable[[], Session], queue: _Queue, tmp_path: Path
    ) -> None:
        entry = _entry(client)
        first = _launch(client, entry)
        _run_all(queue, session_factory, tmp_path)
        with session_factory() as session:
            for run in session.scalars(
                select(Backtest).where(Backtest.sweep_id == uuid.UUID(first))
            ):
                run.engine_version = "0.1.0"
            session.commit()
        second = _launch(client, entry)
        _run_all(queue, session_factory, tmp_path)

        assert set(_origins(session_factory, second).values()) == {None}

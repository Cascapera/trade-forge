"""The backtest worker: where the CPU-bound work the API refuses to do actually runs.

arq drains the queue and calls `run_backtest` in a process separate from the API. The job is
a single row id — everything else is read from the database, which is what makes the worker
idempotent and the enqueue cheap: re-running the same id re-runs the same backtest.

The lifecycle is a state machine written into the `backtests` row: `queued → running → done`
(or `→ failed`, with the reason). Each transition is committed and announced on the run's
progress channel, so a WebSocket subscriber sees the same story the database tells. A failure
is *recorded*, not re-raised — a wrong strategy is a result to report (`GET /backtests/{id}`),
not a job for arq to retry forever.
"""

from __future__ import annotations

import datetime as dt
import json
import uuid
from collections.abc import Callable, Sequence
from decimal import Decimal
from pathlib import Path
from typing import Any

from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.orm import Session

from tradeforge_api.config import RedisConfig, Settings
from tradeforge_api.grid import coordinates, label_for, read_point
from tradeforge_api.queue import progress_channel, redis_settings
from tradeforge_api.runner import ENGINE_VERSION, execute_backtest
from tradeforge_api.walkforward import Candidate, choose
from tradeforge_collector import read_candles
from tradeforge_db.models import (
    Backtest,
    BacktestMetrics,
    BacktestStatus,
    Instrument,
    SelectionMetric,
    Strategy,
    WalkForward,
    WalkForwardFold,
)
from tradeforge_db.results import to_rows
from tradeforge_db.session import create_db_engine, create_session_factory


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


async def _announce(redis: Redis, backtest_id: uuid.UUID, payload: dict[str, Any]) -> None:
    """Publish one progress event. Best-effort: a subscriber that missed it can still read the
    final state from the database, so a publish failure must never fail the run."""
    await redis.publish(progress_channel(backtest_id), json.dumps(payload))


async def process_backtest(
    *,
    session: Session,
    redis: Redis,
    parquet_root: Path,
    backtest_id: uuid.UUID,
) -> None:
    """Run one backtest end to end, driving its row through the status state machine.

    Split from the arq entry point so it can be exercised inline in a test — pass a real
    session and Redis, and this is the whole worker without a running arq process.
    """
    backtest = session.get(Backtest, backtest_id)
    if backtest is None:
        return  # the row was deleted between enqueue and pickup; nothing to run

    try:
        backtest.status = BacktestStatus.RUNNING
        backtest.started_at = _now()
        session.commit()
        await _announce(redis, backtest_id, {"status": "running", "progress": 0.0})

        strategy = session.get(Strategy, backtest.strategy_id)
        instrument = session.get(Instrument, backtest.instrument_id)
        if strategy is None or instrument is None:
            raise ValueError("backtest references a missing strategy or instrument")

        candles = read_candles(parquet_root, instrument.symbol, backtest.timeframe)
        trades, metrics, window = execute_backtest(
            definition=strategy.definition,
            instrument=instrument,
            timeframe=backtest.timeframe,
            date_from=backtest.date_from,
            date_to=backtest.date_to,
            initial_capital=backtest.initial_capital,
            cost_model=backtest.cost_model,
            slippage_ticks=Decimal(0),
            candles=candles,
        )

        metrics_row, trade_rows = to_rows(
            trades=trades,
            metrics=metrics,
            backtest_id=backtest.id,
            instrument_id=instrument.id,
        )
        session.add(metrics_row)
        session.add_all(trade_rows)
        # Recorded on the run, not derived later: the Parquet underneath can be re-collected
        # or extended, and then "what this run read" stops being answerable from the dataset.
        backtest.candles_seen = window.candles
        backtest.first_candle = window.first
        backtest.last_candle = window.last
        backtest.status = BacktestStatus.DONE
        backtest.finished_at = _now()
        session.commit()
        await _announce(redis, backtest_id, {"status": "done", "progress": 1.0})

    except Exception as exc:  # noqa: BLE001 — a failed run is a recorded result, not a crash
        session.rollback()
        _record_failure(session, backtest_id, exc)
        await _announce(redis, backtest_id, {"status": "failed", "error": _reason(exc)})


# --------------------------------------------------------------------------- #
# Walk-forward: the one job in this system that decides something               #
# --------------------------------------------------------------------------- #


_METRIC_OF: dict[SelectionMetric, Callable[[BacktestMetrics], Decimal | None]] = {
    SelectionMetric.NET_PROFIT: lambda metrics: metrics.net_profit,
    SelectionMetric.PROFIT_FACTOR: lambda metrics: metrics.profit_factor,
    SelectionMetric.SHARPE: lambda metrics: metrics.sharpe,
    SelectionMetric.EXPECTANCY: lambda metrics: metrics.expectancy,
}
"""Which column each selection metric reads.

Spelled out rather than `getattr(metrics, walk_forward.metric.value)`, even though the enum's
values were named after the columns on purpose. The reflective version works until a column is
renamed, and then it fails at the moment a fold picks a winner — inside a job, hours into a run
— instead of at the line that is now wrong. Three of the four are nullable, which is the whole
reason `choose` refuses to read a null as a zero.
"""


async def process_walk_forward(
    *,
    session: Session,
    redis: Redis,
    parquet_root: Path,
    walk_forward_id: uuid.UUID,
) -> None:
    """Run every fold in order: train the whole grid, choose, then test the choice.

    ⚠️ **The runs are executed here, inline and sequentially, rather than queued.** The
    alternative — enqueue a fold's training runs and wait for them — deadlocks on a single
    worker: `execute_backtest` is CPU-bound and synchronous, so this coroutine holds the only
    slot the runs it is waiting for would need to claim. Reactive completion (whichever run
    finishes last enqueues the test) trades that for a distributed "who was last?" question
    with a race in it. Sequential is slower and has no failure mode: measured on this project's
    own runs, three hundred backtests is two to four minutes.

    Idempotent by construction, and it has to be: this job is enqueued under the walk-forward's
    own id, so a retry runs it again. Runs already `done` are skipped rather than re-executed —
    re-running one would try to write a second metrics row against the same primary key and
    turn a completed fold into a failed one.
    """
    walk_forward = session.get(WalkForward, walk_forward_id)
    if walk_forward is None:
        return  # the row was deleted between enqueue and pickup; nothing to run
    if walk_forward.status is BacktestStatus.DONE:
        return  # complete; a retry has nothing to add and would restamp a finished clock

    try:
        walk_forward.status = BacktestStatus.RUNNING
        walk_forward.started_at = _now()
        # ⚠️ **Cleared, not left standing, and this is the whole retry path.** An attempt that
        # ended leaves `finished_at` and `error` behind, and the row's own CHECK refuses a
        # finish that precedes its start — so a retry that kept them would die on this very
        # commit with a constraint violation, recorded as the experiment's failure. The message
        # a reader would then see is about a timestamp, and it has *replaced* the reason the
        # experiment failed in the first place.
        walk_forward.finished_at = None
        walk_forward.error = None
        session.commit()

        for fold in walk_forward.fold_rows:
            await _process_fold(
                session=session,
                redis=redis,
                parquet_root=parquet_root,
                walk_forward=walk_forward,
                fold=fold,
            )

        walk_forward.status = BacktestStatus.DONE
        walk_forward.finished_at = _now()
        session.commit()

    except Exception as exc:  # noqa: BLE001 — a failed experiment is a recorded result
        session.rollback()
        _record_walk_forward_failure(session, walk_forward_id, exc)


async def _process_fold(
    *,
    session: Session,
    redis: Redis,
    parquet_root: Path,
    walk_forward: WalkForward,
    fold: WalkForwardFold,
) -> None:
    """One fold: every training run, the choice, and the single run that scores it."""
    if fold.test_backtest_id is not None:
        # Decided and scored by an earlier attempt. Skipped whole rather than re-derived: the
        # training runs below would each be skipped for being `done` anyway, but the run that
        # scores the choice is *created* here, so re-entering writes a **second** out-of-sample
        # run for a fold that already has one. Two answers to "what did this fold make", with
        # nothing on either row to say which one is the fold's.
        return

    training = list(
        session.scalars(
            select(Backtest).where(Backtest.study_id == fold.study_id).order_by(Backtest.id)
        )
    )
    for run in training:
        if run.status is BacktestStatus.DONE:
            continue
        await process_backtest(
            session=session, redis=redis, parquet_root=parquet_root, backtest_id=run.id
        )

    ranked = _candidates(session, fold, training, walk_forward.metric)
    winner = choose(ranked)
    if winner is None:
        # A terminal answer, not a gap: nothing in the grid traded this window, or nothing that
        # traded had a defined score. Recorded by leaving the choice null and moving on — the
        # fold reports itself as undecided, which is a finding about the method.
        return

    fold.chosen_strategy_id = ranked[winner]
    session.commit()

    test = Backtest(
        strategy_id=fold.chosen_strategy_id,
        instrument_id=fold.study.instrument_id,
        timeframe=fold.study.timeframe,
        date_from=fold.test_from,
        date_to=fold.test_to,
        initial_capital=fold.study.initial_capital,
        # The same costs the training runs were charged. A test window scored under a different
        # spread than the window that chose it would make the two sides of this comparison
        # incomparable in the one respect the whole feature is a comparison of.
        cost_model=dict(training[0].cost_model),
        status=BacktestStatus.QUEUED,
        engine_version=ENGINE_VERSION,
    )
    session.add(test)
    session.commit()

    await process_backtest(
        session=session, redis=redis, parquet_root=parquet_root, backtest_id=test.id
    )

    # Linked after the run exists, never before: `test_backtest_id` is a foreign key, and the
    # fold's own CHECK refuses a test run without a choice behind it.
    fold.test_backtest_id = test.id
    session.commit()


def _candidates(
    session: Session,
    fold: WalkForwardFold,
    training: Sequence[Backtest],
    metric: SelectionMetric,
) -> dict[Candidate, uuid.UUID]:
    """Every training run of this fold as something `choose` can rank, mapped to its strategy.

    A mapping rather than a list, because `choose` hands back one candidate and the caller then
    needs the row that produced it. Keyed by the candidate itself — it is frozen, so hashable —
    which keeps the pure ranking type free of any notion of a database id.

    Coordinates come from the document each run actually executed, never from parsing the
    strategy's name: that works until a parameter value contains a comma. They are also the
    tie-break, and they are computed by the same `grid.coordinates` the study read sorts by, so
    "the best point" cannot mean two different things in two endpoints.
    """
    grid: dict[str, list[Any]] = dict(fold.study.grid)
    definitions = {
        strategy.id: strategy.definition
        for strategy in session.scalars(
            select(Strategy).where(Strategy.id.in_({run.strategy_id for run in training}))
        )
    }

    built: dict[Candidate, uuid.UUID] = {}
    for run in training:
        document = definitions.get(run.strategy_id)
        if document is None:  # pragma: no cover — the FK is RESTRICT
            continue
        values = read_point(document, grid)
        candidate = Candidate(
            coordinates=coordinates(grid, values),
            label=label_for(values),
            # `None` when the run failed, has not finished, or the metric is undefined for it.
            # All three are the same thing to a ranking — no score — and none of them is a zero.
            score=None if run.metrics is None else _METRIC_OF[metric](run.metrics),
            trades=0 if run.metrics is None else run.metrics.total_trades,
        )
        built[candidate] = run.strategy_id
    return built


def _record_walk_forward_failure(
    session: Session, walk_forward_id: uuid.UUID, exc: Exception
) -> None:
    walk_forward = session.get(WalkForward, walk_forward_id)
    if walk_forward is None:  # pragma: no cover — deleted mid-run
        return
    walk_forward.status = BacktestStatus.FAILED
    walk_forward.error = _reason(exc)
    if walk_forward.started_at is None:  # pragma: no cover — set before anything can throw
        walk_forward.started_at = _now()
    walk_forward.finished_at = _now()
    session.commit()


def _record_failure(session: Session, backtest_id: uuid.UUID, exc: Exception) -> None:
    backtest = session.get(Backtest, backtest_id)
    if backtest is None:
        return
    backtest.status = BacktestStatus.FAILED
    backtest.error = _reason(exc)
    # `finished_at` may only be set once the run has started (a DB CHECK). If we failed before
    # even marking it running, stamp the start now so the row stays internally consistent.
    if backtest.started_at is None:
        backtest.started_at = _now()
    backtest.finished_at = _now()
    session.commit()


def _reason(exc: Exception) -> str:
    """A driver or engine error can be a multi-line essay; the `error` column wants one line."""
    return " ".join(str(exc).split()) or exc.__class__.__name__


# --------------------------------------------------------------------------- #
# arq wiring                                                                    #
# --------------------------------------------------------------------------- #


async def run_backtest(ctx: dict[str, Any], backtest_id: str) -> None:
    """The registered job. arq passes the run's id as a string; everything else is read from
    the database inside `process_backtest`."""
    session: Session = ctx["session_factory"]()
    settings: Settings = ctx["settings"]
    try:
        await process_backtest(
            session=session,
            redis=ctx["redis"],
            parquet_root=settings.parquet_root,
            backtest_id=uuid.UUID(backtest_id),
        )
    finally:
        session.close()


async def run_walk_forward(ctx: dict[str, Any], walk_forward_id: str) -> None:
    """The registered orchestrating job — one per walk-forward, never one per run.

    ⚠️ **This job occupies a worker slot for the whole experiment**, minutes rather than
    seconds, because it executes every fold's backtests itself. That is deliberate (see
    `process_walk_forward`), and it is why `docker compose up -d --scale worker=4` matters
    here more than anywhere else: with one worker, a running walk-forward means ordinary
    backtests queue behind it.
    """
    session: Session = ctx["session_factory"]()
    settings: Settings = ctx["settings"]
    try:
        await process_walk_forward(
            session=session,
            redis=ctx["redis"],
            parquet_root=settings.parquet_root,
            walk_forward_id=uuid.UUID(walk_forward_id),
        )
    finally:
        session.close()


async def startup(ctx: dict[str, Any]) -> None:
    settings = Settings()
    engine = create_db_engine(settings.sqlalchemy_dsn)
    ctx["settings"] = settings
    ctx["engine"] = engine
    ctx["session_factory"] = create_session_factory(engine)


async def shutdown(ctx: dict[str, Any]) -> None:
    ctx["engine"].dispose()


class WorkerSettings:
    """`arq tradeforge_api.worker.WorkerSettings` starts the worker from this."""

    functions = (run_backtest, run_walk_forward)
    # Built from RedisConfig, not Settings: this line runs at import, and importing the worker
    # must not require the Postgres password. The DB config is read later, in `startup`.
    redis_settings = redis_settings(RedisConfig())
    on_startup = startup
    on_shutdown = shutdown


__all__ = [
    "WorkerSettings",
    "process_backtest",
    "process_walk_forward",
    "run_backtest",
    "run_walk_forward",
]

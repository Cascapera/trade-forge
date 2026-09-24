"""The backtest worker: where the CPU-bound work the API refuses to do actually runs.

arq drains the queue and calls `run_backtest` in a process separate from the API. The job is
a single row id — everything else is read from the database, which is what makes the worker
idempotent and the enqueue cheap: re-running the same id re-runs the same backtest.

The lifecycle is a state machine written into the `backtests` row: `queued → running → done`
(or `→ failed`, with the reason). Each transition is committed and announced on the run's
progress channel, so a WebSocket subscriber sees the same story the database tells. A failure
is *recorded*, not re-raised — a wrong strategy is a result to report (`GET /backtests/{id}`),
not a job for arq to retry forever.

⚠️ **Except when the database cannot be reached at all.** A run that cannot reach Postgres has
not failed; nothing about it was measured. Before this rule, a worker that picked a job while
Postgres was still starting raised out of `session.get`, arq — which does not retry an ordinary
exception — dropped the job, and the row stayed `queued` for ever with nothing to say why
(measured on 15/09, run `77842306`). Now an unreachable database hands the job back to arq with a
growing delay, and only the last try records the run as failed. Every other database error — a
cancelled query, a full disk, a deadlock — is a result, recorded on the first try, provided the
database takes that write. Nothing here can record anything in a database that refuses it.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import json
import uuid
from collections.abc import Callable, Sequence
from decimal import Decimal
from pathlib import Path
from typing import Any

from arq.worker import Retry, func
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError, OperationalError
from sqlalchemy.orm import Session

from tradeforge_api.candle_cache import CandleCache, CandleReader
from tradeforge_api.config import RedisConfig, Settings
from tradeforge_api.grid import coordinates, label_for, read_point
from tradeforge_api.queue import RUN_BACKTEST, progress_channel, redis_settings
from tradeforge_api.retention import recorded_for
from tradeforge_api.runner import ENGINE_VERSION, execute_backtest, instrument_spec
from tradeforge_api.walkforward import Candidate, choose
from tradeforge_collector import read_candles
from tradeforge_db.models import (
    Backtest,
    BacktestMetrics,
    BacktestStatus,
    Collection,
    Instrument,
    SelectionMetric,
    Strategy,
    WalkForward,
    WalkForwardFold,
)
from tradeforge_db.results import ladder_row, to_rows
from tradeforge_db.session import create_db_engine, create_session_factory
from tradeforge_engine.excursion import target_ladder


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


async def _announce(redis: Redis, backtest_id: uuid.UUID, payload: dict[str, Any]) -> None:
    """Publish one progress event. Best-effort: a subscriber that missed it can still read the
    final state from the database, so a publish failure must never fail the run."""
    await redis.publish(progress_channel(backtest_id), json.dumps(payload))


# SQLSTATEs that mean "the server is not there to answer": connection exceptions (class 08), and
# the server shutting down, crashing or still starting (57P01, 57P02, 57P03). 57014, a cancelled
# query, shares class 57 and is deliberately not here — the server answered it.
_UNREACHABLE_STATES = frozenset({"57P01", "57P02", "57P03"})


def database_unreachable(exc: BaseException) -> bool:
    """Whether `exc` says the database could not be reached, as opposed to what it answered.

    ⚠️ psycopg files a cancelled query, a full disk and a deadlock under `OperationalError` too.
    Retrying those would re-run the whole backtest eight times to reach the same answer, so the
    class alone is not the test: the SQLSTATE is. An `OperationalError` with no SQLSTATE comes
    from the connect step — a refused connection, or a server that answered `57P03` at connect
    time, which psycopg reports without the code (the 15/09 case) — and counts as unreachable.
    """
    if not isinstance(exc, DBAPIError):
        return False
    if exc.connection_invalidated:
        return True
    sqlstate: str | None = getattr(exc.orig, "sqlstate", None)
    if sqlstate is None:
        return isinstance(exc, OperationalError)
    return sqlstate.startswith("08") or sqlstate in _UNREACHABLE_STATES


async def process_backtest(  # noqa: PLR0913 — keyword-only; each names one thing the run needs
    *,
    session: Session,
    redis: Redis,
    parquet_root: Path,
    backtest_id: uuid.UUID,
    retry_unreachable: bool = True,
    read: CandleReader = read_candles,
) -> None:
    """Run one backtest end to end, driving its row through the status state machine.

    Split from the arq entry point so it can be exercised inline in a test — pass a real
    session and Redis, and this is the whole worker without a running arq process.

    `retry_unreachable=False` is for a caller that cannot retry — the walk-forward runs its folds
    inline — and records an unreachable database as the run's failure, as before, rather than
    leaving the run `running` with nobody coming back for it.

    `read` is where the bars come from: the worker passes its `CandleCache`, so a sweep's runs
    over one symbol read the Parquet once rather than once each (`candle_cache`).
    """
    # ⚠️ Outside the `try`, on purpose: a database that cannot answer this is not a failed run,
    # and the error must reach the caller — `run_backtest`, which retries or records it.
    backtest = session.get(Backtest, backtest_id)
    if backtest is None:
        return  # the row was deleted between enqueue and pickup; nothing to run
    if backtest.status in (BacktestStatus.DONE, BacktestStatus.FAILED):
        # A retry after a commit whose outcome was lost with the connection: the run already has
        # its result. Running a `done` one again would write a second metrics row against the same
        # key; restarting a `failed` one would stamp a start after its recorded finish, and the
        # CHECK that refuses that would replace the real reason with its own. Nothing enqueues a
        # finished run on purpose — every launch writes a new row.
        return

    try:
        backtest.status = BacktestStatus.RUNNING
        backtest.started_at = _now()
        session.commit()
        await _announce(redis, backtest_id, {"status": "running", "progress": 0.0})

        strategy = session.get(Strategy, backtest.strategy_id)
        instrument = session.get(Instrument, backtest.instrument_id)
        if strategy is None or instrument is None:
            raise ValueError("backtest references a missing strategy or instrument")

        candles = read(parquet_root, instrument.symbol, backtest.timeframe)
        # A sweep's run keeps no pictures, so it does not build them either (`retention`).
        in_sweep = backtest.sweep_id is not None
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
            record_snapshots=not in_sweep,
        )

        # Every rung of the target ladder, scored now: a sweep's losing run keeps no trades, and
        # without them this cannot be computed later (`backtest_metrics.targets`).
        ladder = target_ladder(trades, instrument_spec(instrument))
        # Decided here and stamped on the run, never re-derived later: see `Recorded`.
        recorded = recorded_for(
            in_sweep=in_sweep,
            timeframe=backtest.timeframe,
            net_profit=metrics.net_profit,
            total_trades=metrics.total_trades,
            target_net_r=[None if rung is None else rung.net_r for rung in ladder.values()],
        )
        metrics_row, trade_rows = to_rows(
            trades=trades,
            metrics=metrics,
            backtest_id=backtest.id,
            instrument_id=instrument.id,
            recorded=recorded,
        )
        metrics_row.targets = ladder_row(ladder)
        backtest.recorded = recorded
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

    except DBAPIError as exc:
        if retry_unreachable and database_unreachable(exc):
            # The database, not the run: nothing was measured, so nothing is recorded. The caller
            # hands the job back to arq.
            raise
        session.rollback()
        _record_failure(session, backtest_id, exc)
        await _announce(redis, backtest_id, {"status": "failed", "error": _reason(exc)})
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
    read: CandleReader = read_candles,
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
                read=read,
            )

        walk_forward.status = BacktestStatus.DONE
        walk_forward.finished_at = _now()
        session.commit()

    except Exception as exc:  # noqa: BLE001 — a failed experiment is a recorded result
        session.rollback()
        _record_walk_forward_failure(session, walk_forward_id, exc)


async def _process_fold(  # noqa: PLR0913 — keyword-only; each names one thing the fold needs
    *,
    session: Session,
    redis: Redis,
    parquet_root: Path,
    walk_forward: WalkForward,
    fold: WalkForwardFold,
    read: CandleReader,
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
            session=session,
            redis=redis,
            parquet_root=parquet_root,
            backtest_id=run.id,
            retry_unreachable=False,
            read=read,
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
        session=session,
        redis=redis,
        parquet_root=parquet_root,
        backtest_id=test.id,
        retry_unreachable=False,
        read=read,
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
    if backtest.status is BacktestStatus.DONE:
        # A result already landed — a commit whose answer was lost, or an error raised after it
        # (the progress publish). What failed afterwards is not the run, and must not erase it.
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


WAIT_POLL_SECONDS = 30
"""How long a run waiting for its collection sleeps before asking again.

A collection advances one calendar year at a time and a cold year takes minutes on this broker,
so asking every second would be hundreds of questions about a row that changes five times."""

WAIT_LIMIT = dt.timedelta(hours=2)
"""How long the **collection queue** may go silent before a run waiting on it is failed.

⚠️ **Measured from the queue's last delivery, not from the run's birth.** The host agent
downloads one collection at a time (`max_jobs = 1`), and a basket may ask for twenty markets in
one click: the last of them has not started hours in, though nothing is wrong. A clock started at
the launch would fail those runs on a healthy system — so what is timed is silence, and every
collection that lands anywhere resets it.

⚠️ **A ceiling on the wait, not an estimate of it.** Two hours of nothing finishing anywhere is
an agent that stopped or a terminal nobody logged in to — the failure this limit exists for,
and the one this project has already paid for once (the run stuck `queued` on 15/09)."""

MAX_TRIES = 8
"""How many times a backtest job runs before a database that will not answer is its result.

With `retry_delay`, the seven waits add up to 140 seconds — room for a database that is starting.
That is the waiting alone: each try also spends however long its own connect takes, and no
connect timeout is set. A database that comes back within the budget gets the run recorded,
done or failed; one that never does leaves the row as it was, with the error in arq's result."""


_ENDED = frozenset({BacktestStatus.DONE, BacktestStatus.FAILED})
"""The states a collection never leaves, and so the ones a waiting run stops waiting on."""


def retry_delay(job_try: int) -> int:
    """Seconds to wait before try `job_try + 1`: 5, 10, 15 … — room for a database to start."""
    return 5 * job_try


async def _still_collecting(ctx: dict[str, Any], session: Session, run_id: uuid.UUID) -> bool:
    """Is this run waiting for data, and has the waiting been dealt with?

    True means the caller must stop: the run was either deferred (a download is still going) or
    failed (the wait outlived `WAIT_LIMIT`). False means there is nothing left to wait for — no
    collection was ever linked, or every one of them has ended, done **or failed**.

    ⚠️ **A failed download does not fail the run — his rule of 22/09.** It used to: *"half a window
    is not a shorter measurement, it is a different one"*. He chose the other side of that: the run
    goes ahead with what is on disk and **says** which download failed and why. Nothing here has to
    write that down: the link row stays and `failed` is terminal (`finish_collection` closes a row
    once), so every reader — the run's screen, a basket's, a sweep's — derives the warning from
    `waiting_for`. And the window the run actually covered is already reported beside the one that
    was asked for (`coverage`), which is what keeps the shorter measurement from passing as the
    requested one. A run left with nothing at all on disk still fails, on the data, as it should.

    ⚠️ **Deferred by enqueuing a new job, never by `Retry`.** `run_backtest`'s retry budget exists
    for a database that is not answering, and it is eight tries; a wait of two hours is two
    hundred and forty. Spending one budget on the other would make an unreachable database look
    like a slow download, and would run out long before the data landed.

    ⚠️ **On the run's own session, inside the caller's `try`.** A database that stops answering
    while this question is being asked is the caller's case, not a new one, and answering it here
    would be a second error policy for the same failure.
    """
    run = session.get(Backtest, run_id)
    if run is None:
        return False
    # The run's own relationship, so the worker and the screen read one definition of "waiting
    # for" — including its order — rather than two queries free to drift apart.
    waits = run.waiting_for
    if not waits:
        return False

    # Ended is done or failed: a failed download is waited for no longer, and the run reads what is
    # there. The siblings still downloading are waited for as before — the run takes all it can get.
    unfinished = [one for one in waits if one.status not in _ENDED]
    if not unfinished:
        return False

    # The queue's last sign of life, or this run's birth when nothing has ever landed.
    # ⚠️ Ordered rather than `func.max`: arq's own `func` registers this module's jobs at
    # the bottom of the file, and importing SQLAlchemy's `func` over it breaks the worker at
    # import time — which is how this was found.
    last_delivery = session.scalar(
        select(Collection.finished_at)
        .where(Collection.finished_at.is_not(None))
        .order_by(Collection.finished_at.desc())
        .limit(1)
    )
    silent_since = max(run.created_at, last_delivery) if last_delivery else run.created_at
    if _now() - silent_since > WAIT_LIMIT:
        names = ", ".join(f"{one.symbol} {one.timeframe}" for one in unfinished)
        _fail(
            session,
            run,
            f"nothing has been collected for {WAIT_LIMIT} while waiting for {names}; gave up",
        )
        await _announce(ctx["redis"], run_id, {"status": "failed", "error": run.error})
        return True

    await ctx["redis"].enqueue_job(
        RUN_BACKTEST, str(run_id), _defer_by=dt.timedelta(seconds=WAIT_POLL_SECONDS)
    )
    return True


def _fail(session: Session, run: Backtest, reason: str) -> None:
    """Record a run that ends before it ever reads a candle.

    ⚠️ `started_at` is stamped too, for `_record_failure`'s reason: the database refuses a row
    that finished without starting, and a run failed while waiting has genuinely never run.
    """
    run.status = BacktestStatus.FAILED
    run.error = reason
    if run.started_at is None:
        run.started_at = _now()
    run.finished_at = _now()
    session.commit()


async def run_backtest(ctx: dict[str, Any], backtest_id: str) -> None:
    """The registered job. arq passes the run's id as a string; everything else is read from
    the database inside `process_backtest`.

    ⚠️ **The last try is ours, not arq's.** Past `max_tries` arq refuses the job without calling
    this function, so a retry budget spent by arq would leave the row exactly where the bug left
    it. On the last try the unreachable database is recorded as the run's failure — if the
    database answers that write. If it does not, that write's error goes to arq's own result (the
    original rides along as its context), and the row stays as it was: nothing here can write to
    a database that is not there.

    A database error that is **not** unreachability — a cancelled query on the first read — is
    recorded the same way, on the first try: it is an answer, and retrying would get it again.
    """
    run_id = uuid.UUID(backtest_id)
    session: Session = ctx["session_factory"]()
    settings: Settings = ctx["settings"]
    try:
        # ⚠️ Before anything is read for the run: a run told to collect first has no candles yet,
        # and starting it would record "no candles in this window" for data on its way.
        if await _still_collecting(ctx, session, run_id):
            return
        await process_backtest(
            session=session,
            redis=ctx["redis"],
            parquet_root=settings.parquet_root,
            backtest_id=run_id,
            read=ctx["candles"].read,
        )
    except DBAPIError as exc:
        job_try: int = ctx.get("job_try", 1)
        if database_unreachable(exc) and job_try < MAX_TRIES:
            raise Retry(defer=retry_delay(job_try)) from exc
        # ⚠️ **The old session is let go before the new one is opened.** If its connection were
        # still alive inside an aborted transaction, it would hold this row's lock until ROLLBACK,
        # and the new session's UPDATE would wait on it — synchronously, stalling the worker's
        # whole event loop. Not reproduced against a real server; the order costs nothing.
        _discard(session)
        last = ctx["session_factory"]()
        try:
            _record_failure(last, run_id, exc)
        finally:
            last.close()
        await _announce(ctx["redis"], run_id, {"status": "failed", "error": _reason(exc)})
    finally:
        session.close()


def _discard(session: Session) -> None:
    """Roll back and close, tolerating a connection that is already gone."""
    # Nothing to roll back on a dead connection; closing hands it back, and the pool discards an
    # invalidated one rather than reusing it.
    with contextlib.suppress(DBAPIError):
        session.rollback()
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
            read=ctx["candles"].read,
        )
    finally:
        session.close()


async def startup(ctx: dict[str, Any]) -> None:
    settings = Settings()
    engine = create_db_engine(settings.sqlalchemy_dsn)
    ctx["settings"] = settings
    ctx["engine"] = engine
    ctx["session_factory"] = create_session_factory(engine)
    # One per worker process, for as long as it lives (`candle_cache`).
    ctx["candles"] = CandleCache()


async def shutdown(ctx: dict[str, Any]) -> None:
    ctx["engine"].dispose()


class WorkerSettings:
    """`arq tradeforge_api.worker.WorkerSettings` starts the worker from this."""

    # `run_backtest` spends the last try itself, so arq must be told the same number — stated on
    # that function alone. The walk-forward keeps arq's default: it does not raise `Retry`.
    functions = (func(run_backtest, max_tries=MAX_TRIES), run_walk_forward)
    # Built from RedisConfig, not Settings: this line runs at import, and importing the worker
    # must not require the Postgres password. The DB config is read later, in `startup`.
    redis_settings = redis_settings(RedisConfig())
    # ⚠️ **One job at a time** (23/09). arq's default is ten, which suits I/O; a backtest is
    # synchronous CPU that blocks the event loop, so the other nine jobs this worker took would
    # sit claimed and idle behind it — while another worker has nothing — and run into arq's job
    # timeout without ever starting. More speed comes from more workers (`TRADEFORGE_WORKERS`).
    max_jobs = 1
    # ⚠️ **A twentieth of a second, not arq's half** (24/09). The run is synchronous CPU, so it
    # holds the event loop through the poll that falls due while it runs, and the slot it frees is
    # only seen on the poll after that: every run paid one whole `poll_delay` before the next one
    # started. Measured on a CHOCH sweep, 0.508 s between runs of 1.24 s — 29% of the worker idle.
    # The cost is an idle worker asking Redis twenty times a second instead of two, which is
    # nothing to Redis even with forty workers asking.
    poll_delay = 0.05
    on_startup = startup
    on_shutdown = shutdown


__all__ = [
    "MAX_TRIES",
    "WorkerSettings",
    "database_unreachable",
    "process_backtest",
    "process_walk_forward",
    "retry_delay",
    "run_backtest",
    "run_walk_forward",
]

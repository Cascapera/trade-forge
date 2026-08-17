"""`/walkforwards` — a study re-asked honestly: choose on one window, score on the next.

`/studies` can find the best point of a grid and says so in its own docstring; what it cannot
do is say whether that point is worth anything, because it searched and scored on the same
candles. Every figure it reports is in-sample, including the best one. This router is where
that gap is closed.

`POST` cuts the study's period into folds by **counting candles**, writes one `Study` per fold
over its training window, queues that fold's grid, and hands a single orchestrating job to the
queue. `GET` reads the folds back with the choice each one made and what the choice was worth
on each side of the line.

⚠️ **The training runs are written but never enqueued.** They are executed by the orchestrator
in order, inside one worker slot — see `queue.RUN_WALK_FORWARD` for why the obvious
alternative deadlocks.
"""

import uuid
from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from tradeforge_api.deps import QueueDep, SessionDep, SettingsDep
from tradeforge_api.grid import label_for, read_point
from tradeforge_api.queue import RUN_WALK_FORWARD
from tradeforge_api.routers.studies import points_for, strategies_for
from tradeforge_api.runner import ENGINE_VERSION
from tradeforge_api.schemas import (
    CreatedWalkForward,
    CreateWalkForwardRequest,
    WalkForwardFoldOut,
    WalkForwardOut,
    WalkForwardVerdict,
)
from tradeforge_api.walkforward import MAX_RUNS, Fold, Outcome, WalkForwardError, split, verdict
from tradeforge_collector import read_times
from tradeforge_db.models import (
    Backtest,
    BacktestMetrics,
    BacktestStatus,
    Instrument,
    Strategy,
    Study,
    WalkForward,
    WalkForwardFold,
)

router = APIRouter(tags=["walkforwards"])

_Responses = dict[int | str, dict[str, Any]]
_NOT_FOUND: _Responses = {status.HTTP_404_NOT_FOUND: {"description": "not found"}}


def _folds_for(
    settings: SettingsDep, study: Study, symbol: str, request: CreateWalkForwardRequest
) -> list[Fold]:
    """Cut the study's period into train→test pairs, reading only the candles' instants.

    ⚠️ **Read from the Parquet, not computed from the study's dates.** `date_from` and
    `date_to` are what somebody *asked for*; the dataset underneath may open later or end
    earlier, and cutting a request into six even parts when the data fills four of them
    produces two folds with no candles in them at all. The same distinction `candles_seen`
    exists to record, applied before the runs rather than after.

    Only the `time` column is read — `read_times`, not `read_candles` — because nothing here
    looks at a price. Parquet stores columns separately, so the other six never leave disk.
    """
    times = read_times(
        settings.parquet_root, symbol, study.timeframe, start=study.date_from, end=study.date_to
    )
    if not times:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=(
                f"{symbol} {study.timeframe} has no candles in this study's window; "
                f"the study cannot be re-run out of sample"
            ),
        )
    try:
        return split(
            times,
            folds=request.folds,
            train_multiple=request.train_multiple,
            anchored=request.anchored,
        )
    except WalkForwardError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc


def _cost_model_of(session: SessionDep, study: Study) -> dict[str, Any]:
    """The cost model the study's own runs were charged, read back off one of them.

    `studies` has no `cost_model` column, deliberately: the points share one model by
    construction, and storing it on the group as well would create a second place for it to be
    true. So the honest source is a run — and it has to be *this* study's run, because charging
    the walk-forward a different spread than the heatmap was drawn with would make the two
    incomparable in the one respect the whole feature is a comparison of.

    Ordered by id purely so the answer does not depend on which row Postgres returned first.
    Every run of a study is written from one request with one model, so any of them answers.
    """
    model = session.scalar(
        select(Backtest.cost_model)
        .where(Backtest.study_id == study.id)
        .order_by(Backtest.id)
        .limit(1)
    )
    if model is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="that study has no runs, so there is no cost model to reproduce",
        )
    return dict(model)


@router.post(
    "/walkforwards",
    response_model=CreatedWalkForward,
    status_code=status.HTTP_202_ACCEPTED,
    responses=_NOT_FOUND,
)
async def create_walk_forward(
    request: CreateWalkForwardRequest,
    session: SessionDep,
    settings: SettingsDep,
    queue: QueueDep,
) -> CreatedWalkForward:
    """Cut the folds, write every training run, and hand one job to the queue.

    Everything is written in one transaction and validated before any of it: the folds have to
    be cuttable, the grid has to expand against this strategy, and every resulting document has
    to pass the same DSL validator `POST /strategies` uses. A walk-forward that half-exists is
    worse than one that was refused — the caller asked one question about a method, and three
    hundred runs plus an error answers a question nobody asked.
    """
    study = session.get(Study, request.study_id)
    if study is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="study not found")

    base = session.get(Strategy, study.strategy_id)
    instrument = session.get(Instrument, study.instrument_id)
    if base is None or instrument is None:  # pragma: no cover — both FKs are RESTRICT
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="study references nothing"
        )

    grid: dict[str, list[Any]] = dict(study.grid)
    points = points_for(base, grid)
    folds = _folds_for(settings, study, instrument.symbol, request)

    # ⚠️ The multiplication is the whole point of checking here rather than trusting the two
    # limits separately. A grid inside `MAX_POINTS` and a fold count inside `MAX_FOLDS` still
    # multiply out past anything worth running, and neither number looks alarming on its own.
    total_runs = len(points) * len(folds)
    if total_runs > MAX_RUNS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=(
                f"{len(points)} grid points over {len(folds)} folds is {total_runs} backtests, "
                f"over the {MAX_RUNS} one walk-forward will run"
            ),
        )

    cost_model = _cost_model_of(session, study)

    walk_forward = WalkForward(
        study_id=study.id,
        folds=request.folds,
        train_multiple=request.train_multiple,
        anchored=request.anchored,
        metric=request.metric,
        status=BacktestStatus.QUEUED,
    )
    session.add(walk_forward)

    # ⚠️ **One set of strategies for every fold, and it falls out of a PR-203 decision.** A
    # point's document carries no symbol and no dates — those live on the run — so `period=9`
    # is byte-identical in fold 1 and fold 6, and `strategies_for` reuses the stored row.
    # Six folds over fifty points is fifty strategies, not three hundred.
    strategies = strategies_for(session, points)
    session.add_all(strategies)
    session.flush()

    # Collected as they are built rather than read back off `walk_forward.fold_rows`: the
    # relationship is ordered by index in SQL, but in memory it is whatever order things were
    # appended in, and the response below pairs these with `folds` positionally.
    fold_rows: list[WalkForwardFold] = []

    for fold in folds:
        fold_study = Study(
            strategy_id=base.id,
            instrument_id=instrument.id,
            timeframe=study.timeframe,
            # The training window, and this row is the only place it is written. The fold points
            # here for it rather than copying it, so a fold cannot claim one window while the
            # runs underneath it executed another.
            date_from=fold.train.start,
            date_to=fold.train.end,
            initial_capital=study.initial_capital,
            grid=grid,
        )
        session.add(fold_study)
        session.flush()

        session.add_all(
            Backtest(
                study=fold_study,
                strategy_id=strategy.id,
                instrument_id=instrument.id,
                timeframe=study.timeframe,
                date_from=fold.train.start,
                date_to=fold.train.end,
                initial_capital=study.initial_capital,
                cost_model=cost_model,
                status=BacktestStatus.QUEUED,
                engine_version=ENGINE_VERSION,
            )
            for strategy in strategies
        )
        fold_row = WalkForwardFold(
            walk_forward=walk_forward,
            index=fold.index,
            study_id=fold_study.id,
            test_from=fold.test.start,
            test_to=fold.test.end,
            train_bars=fold.train.bars,
            test_bars=fold.test.bars,
        )
        session.add(fold_row)
        fold_rows.append(fold_row)

    try:
        session.commit()
    except IntegrityError as exc:
        # Two identical grids launched at the same moment, exactly as `POST /studies` can
        # collide: both read the existing strategies before either wrote, and the loser blocks
        # on the unique `(name, version)` until the winner commits. The rollback is total, so
        # the honest reply is that the strategies were created a moment ago and asking again
        # will find and reuse them.
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="another experiment created these strategies at the same time; try again",
        ) from exc

    # One job for the whole thing, after the commit. See `queue.RUN_WALK_FORWARD`: the training
    # runs are deliberately not enqueued, and `_job_id` makes this enqueue idempotent, so a
    # request that died here can be retried rather than reconciled by hand.
    await queue.enqueue_job(RUN_WALK_FORWARD, str(walk_forward.id), _job_id=str(walk_forward.id))

    return CreatedWalkForward(
        id=walk_forward.id,
        runs_queued=total_runs,
        folds=[
            _fold_out(row, fold, chosen=None, in_sample=None, test=None, grid=grid)
            for row, fold in zip(fold_rows, folds, strict=True)
        ],
    )


def _return_of(run: Backtest | None, capital: Decimal) -> Decimal | None:
    """A run's net profit as a fraction of the capital it started with, or `None`.

    `None` covers a run that has not finished and a run that failed, and it stays `None` rather
    than becoming zero: zero is a measured result of no profit, and a fold that has not landed
    has measured nothing. The same distinction the study's aggregate draws.

    Metrics rather than `status == done`, because a run is marked done an instant before its
    metrics row is written — reading `net_profit` off that gap is an exception raised by a
    reporting endpoint.
    """
    if run is None or run.metrics is None:
        return None
    return run.metrics.net_profit / capital


def _fold_out(  # noqa: PLR0913 — keyword-only; each names one row this fold is assembled from
    row: WalkForwardFold,
    fold: Fold | None,
    *,
    chosen: Strategy | None,
    in_sample: Backtest | None,
    test: Backtest | None,
    grid: Mapping[str, Sequence[Any]],
    capital: Decimal = Decimal(1),
) -> WalkForwardFoldOut:
    """One fold as it goes on the wire, from the row plus the runs on either side of its line.

    `fold` is passed only on the creation path, where the row's own study has not been loaded
    back yet; on the read path the training window comes from that study, which is where it
    lives. Two sources for one field looks like a smell and is the opposite: the window is
    written in exactly one place, and this is the two ways of reaching that place.
    """
    label = None if chosen is None else label_for(read_point(chosen.definition, grid))
    train_from = fold.train.start if fold is not None else row.study.date_from
    train_to = fold.train.end if fold is not None else row.study.date_to

    return WalkForwardFoldOut(
        index=row.index,
        study_id=row.study_id,
        train_from=train_from,
        train_to=train_to,
        test_from=row.test_from,
        test_to=row.test_to,
        train_bars=row.train_bars,
        test_bars=row.test_bars,
        chosen_label=label,
        chosen_strategy_id=row.chosen_strategy_id,
        test_backtest_id=row.test_backtest_id,
        in_sample_return=_return_of(in_sample, capital),
        out_of_sample_return=_return_of(test, capital),
        test_status=None if test is None else test.status.value,
        test_trades=None if test is None or test.metrics is None else test.metrics.total_trades,
    )


@router.get("/walkforwards/{walk_forward_id}", response_model=WalkForwardOut, responses=_NOT_FOUND)
def get_walk_forward(walk_forward_id: uuid.UUID, session: SessionDep) -> WalkForwardOut:
    """The walk-forward, every fold's decision, and what the folds add up to.

    Three queries beyond the row itself, and the shape of them is the design. The obvious read
    — every run of every fold — is three hundred rows to report six; instead only the *chosen*
    point's training run is fetched per fold, plus the out-of-sample runs. Each fold's full
    grid stays reachable through `GET /studies/{id}`, which is where a reader who wants the
    heatmap goes anyway.
    """
    walk_forward = session.get(
        WalkForward,
        walk_forward_id,
        options=[selectinload(WalkForward.fold_rows).selectinload(WalkForwardFold.study)],
    )
    if walk_forward is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="walk-forward not found")

    study = session.get(Study, walk_forward.study_id)
    if study is None:  # pragma: no cover — the FK is RESTRICT
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="walk-forward references nothing"
        )
    base = session.get(Strategy, study.strategy_id)
    instrument = session.get(Instrument, study.instrument_id)
    if base is None or instrument is None:  # pragma: no cover — both FKs are RESTRICT
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="walk-forward references nothing"
        )

    rows = list(walk_forward.fold_rows)
    grid: dict[str, list[Any]] = dict(study.grid)
    chosen = _chosen_strategies(session, rows)
    in_sample = _in_sample_runs(session, rows)
    tests = _test_runs(session, rows)

    folds = [
        _fold_out(
            row,
            None,
            chosen=chosen.get(row.chosen_strategy_id) if row.chosen_strategy_id else None,
            in_sample=in_sample.get((row.study_id, row.chosen_strategy_id)),
            test=tests.get(row.test_backtest_id) if row.test_backtest_id else None,
            grid=grid,
            capital=study.initial_capital,
        )
        for row in rows
    ]

    return WalkForwardOut(
        id=walk_forward.id,
        study_id=study.id,
        strategy_id=study.strategy_id,
        strategy_name=base.name,
        symbol=instrument.symbol,
        timeframe=study.timeframe,
        initial_capital=study.initial_capital,
        grid=grid,
        folds_requested=walk_forward.folds,
        train_multiple=walk_forward.train_multiple,
        anchored=walk_forward.anchored,
        metric=walk_forward.metric.value,
        status=walk_forward.status.value,
        error=walk_forward.error,
        created_at=walk_forward.created_at,
        started_at=walk_forward.started_at,
        finished_at=walk_forward.finished_at,
        folds=folds,
        verdict=WalkForwardVerdict.model_validate(
            verdict(
                [
                    Outcome(
                        index=fold.index,
                        label=fold.chosen_label,
                        in_sample=fold.in_sample_return,
                        out_of_sample=fold.out_of_sample_return,
                    )
                    for fold in folds
                ]
            ),
            from_attributes=True,
        ),
    )


def _chosen_strategies(
    session: SessionDep, rows: Sequence[WalkForwardFold]
) -> dict[uuid.UUID, Strategy]:
    """The documents of the points the folds chose, so each fold can be named by its values.

    Named from the document rather than from the stored strategy's name, which carries the base
    strategy in front of the values (`MME9 [period=9]`). Serving that here while a study serves
    `period=9` would leave a client unable to match a fold's choice to a heatmap cell — the
    same defect `label_for` was extracted to close.
    """
    ids = {row.chosen_strategy_id for row in rows if row.chosen_strategy_id is not None}
    if not ids:
        return {}
    return {
        strategy.id: strategy
        for strategy in session.scalars(select(Strategy).where(Strategy.id.in_(ids)))
    }


def _in_sample_runs(
    session: SessionDep, rows: Sequence[WalkForwardFold]
) -> dict[tuple[uuid.UUID, uuid.UUID | None], Backtest]:
    """Each chosen point's *training* run, keyed by the fold's study and the strategy.

    Keyed by the pair and not by the strategy alone, and that is not defensive: the folds share
    one set of strategies by construction, so `period=9` has a run in every fold's study. A map
    keyed by strategy would hold whichever fold's run was read last and quietly report the same
    in-sample number six times.
    """
    pairs = [
        (row.study_id, row.chosen_strategy_id) for row in rows if row.chosen_strategy_id is not None
    ]
    if not pairs:
        return {}
    runs = session.scalars(
        select(Backtest)
        .where(
            Backtest.study_id.in_({study_id for study_id, _ in pairs}),
            Backtest.strategy_id.in_({strategy_id for _, strategy_id in pairs}),
        )
        .options(selectinload(Backtest.metrics).defer(BacktestMetrics.equity_curve))
    )
    found = {(run.study_id, run.strategy_id): run for run in runs if run.study_id is not None}
    return {pair: found[pair] for pair in pairs if pair in found}


def _test_runs(session: SessionDep, rows: Sequence[WalkForwardFold]) -> dict[uuid.UUID, Backtest]:
    """The out-of-sample runs, with the equity curve deferred — six curves nothing here reads."""
    ids = {row.test_backtest_id for row in rows if row.test_backtest_id is not None}
    if not ids:
        return {}
    runs = session.scalars(
        select(Backtest)
        .where(Backtest.id.in_(ids))
        .options(selectinload(Backtest.metrics).defer(BacktestMetrics.equity_curve))
    )
    return {run.id: run for run in runs}


__all__ = ["router"]

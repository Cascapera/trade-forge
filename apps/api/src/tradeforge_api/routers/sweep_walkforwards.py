"""A sweep's walk-forward (25/09, path B): launched from a finished sweep, read back fold by fold.

Each fold's training is an ordinary sweep (`launch_window`) and each fold's test an ordinary
reserved-window test (`launch_holdout`), so they are queued, read, sliced and resampled by
everything those already have. This router ties them together and reads them as one answer.

By cut (01/10, `mode: "cut"`) nothing is launched: every fold is answered from the sweep's own runs
cut to whole years (`sweep_walkforward_cut`), in the request, and kept on the folds.
"""

import asyncio
import datetime as dt
import uuid
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from tradeforge_api.batching import enqueue_runs
from tradeforge_api.deps import QueueDep, SessionDep, SettingsDep
from tradeforge_api.holdout import HoldoutRank, WindowUse, behaviour, reusing
from tradeforge_api.queue import RUN_SWEEP_WALK_FORWARD
from tradeforge_api.ranking_floor import RANK_MIN_TRADES
from tradeforge_api.routers.sweeps import (
    _points_of,
    _runs_of,
    get_holdout,
    jobs_for,
    launch_window,
    retest_rule,
    window_used,
    window_uses,
)
from tradeforge_api.schemas import (
    CreatedSweepWalkForward,
    CreateSweepWalkForward,
    SweepWalkForwardFoldOut,
    SweepWalkForwardGroupOut,
    SweepWalkForwardOut,
)
from tradeforge_api.sweep_walkforward import MIN_FOLDS, FoldGroup, Window, stability, windows
from tradeforge_api.sweep_walkforward_cut import (
    CutRule,
    CutRun,
    Excluded,
    fold_cut,
    fold_document,
    uncovered_years,
)
from tradeforge_api.sweep_walkforward_job import settled
from tradeforge_db.models import (
    Backtest,
    BacktestStatus,
    CatalogEntry,
    Sweep,
    SweepWalkForward,
    SweepWalkForwardFold,
)

router = APIRouter(tags=["sweeps"])

_NOT_FOUND: dict[int | str, dict[str, Any]] = {
    status.HTTP_404_NOT_FOUND: {"description": "not found"}
}

FIRST_LOOK = dt.timedelta(seconds=30)
"""When the conductor first looks, and how often after (`sweep_walkforward_job`)."""


@router.post(
    "/sweeps/{sweep_id}/walkforward",
    response_model=CreatedSweepWalkForward,
    status_code=status.HTTP_202_ACCEPTED,
    responses=_NOT_FOUND,
)
async def create_sweep_walk_forward(
    sweep_id: uuid.UUID,
    request: CreateSweepWalkForward,
    session: SessionDep,
    queue: QueueDep,
    settings: SettingsDep,
) -> CreatedSweepWalkForward:
    """Keep the walk-forward, launch every fold's training at once, and start the conductor.

    ⚠️ **Refused for a reserved-window test** — its points were chosen elsewhere, and walking it
    forward would re-choose among a handful — and for windows whose first test starts in the
    future, which would test nothing.

    ⚠️ **Refused with a 409 when a test window was already used** by an earlier test of this
    sweep, or a fold of an earlier walk-forward of it (01/10) — unless the request says `retest`.
    """
    parent = session.get(Sweep, sweep_id)
    if parent is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="sweep not found")
    if parent.holdout_rule is not None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="a reserved-window test cannot walk forward; walk the sweep it came from",
        )
    charts = None
    if request.timeframes is not None:
        unknown = [one for one in request.timeframes if one not in parent.timeframes]
        if unknown:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=f"the sweep has no {', '.join(unknown)} to walk; it ran "
                + ", ".join(parent.timeframes),
            )
        # In the sweep's own order, whatever order they were asked in.
        charts = [one for one in parent.timeframes if one in request.timeframes]
    try:
        planned = windows(
            start_year=request.start_year,
            train_years=request.train_years,
            test_years=request.test_years,
            folds=request.folds,
            anchored=request.anchored,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc
    # ⚠️ **Every fold, not the first** (28/09). A fold whose test starts after today trains on
    # the same data as the last fold that can be tested — the training is cut at today — and has
    # nothing to test: the walk-forward of that morning queued three such folds, 360 thousand
    # runs for no answer. Refused, with how many fit.
    now = dt.datetime.now(tz=dt.UTC)
    future = [index for index, window in enumerate(planned) if window.test_from >= now]
    if future:
        fit = future[0]
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"fold {future[0] + 1} would test from "
            f"{planned[future[0]].test_from:%Y-%m-%d}, in the future: "
            + (
                f"with these windows at most {fit} folds fit"
                if fit >= MIN_FOLDS
                else "these windows leave fewer than two folds to test"
            ),
        )

    if request.mode == "cut":
        _check_cut(request, parent, planned)

    # ⚠️ **Each test window weighed against the sweep's earlier looks, before anything is kept**
    # (01/10): a walk-forward that tests years an earlier test of this sweep already used is a
    # 409, as a reserved-window test is (`launch_holdout`). Its own folds are not weighed against
    # each other: their tests tile, so one span from the first to the last is exactly their union.
    prior = reusing(planned[0].test_from, planned[-1].test_to, window_uses(session, parent.id))
    if prior and not request.retest:
        raise window_used(prior, what="a test window of this walk-forward")

    if request.mode == "cut":
        # ⚠️ In a thread: a sweep of a hundred thousand runs is read and cut in one go, and
        # the event loop answers every other request meanwhile — as a sweep's launch does.
        walk = await asyncio.to_thread(
            _walk_by_cut, session, parent, request, planned, charts, prior
        )
        return CreatedSweepWalkForward(id=walk.id, folds=len(planned), runs=0)

    rule = request.model_dump(
        mode="json",
        include={
            "top_n",
            "metric",
            "min_trades",
            "max_drawdown_r",
            "min_positive_year_share",
            "distinct",
        },
        exclude_none=True,
    )
    # A retest is marked on the walk-forward too, with the looks it repeats; each fold's test
    # carries its own (`sweep_walkforward_job.advance`).
    rule |= retest_rule(prior)
    walk = SweepWalkForward(
        parent_sweep_id=parent.id,
        start_year=request.start_year,
        train_years=request.train_years,
        test_years=request.test_years,
        anchored=request.anchored,
        rule=rule,
        status=BacktestStatus.QUEUED,
    )
    session.add(walk)
    session.commit()

    queued: list[Backtest] = []
    by_fold: list[list[Backtest]] = []
    for index, window in enumerate(planned):
        training, runs = launch_window(
            session, parent, window.train_from, window.train_to, timeframes=charts
        )
        queued += runs
        by_fold.append(runs)
        session.add(
            SweepWalkForwardFold(
                walk_forward_id=walk.id,
                index=index,
                train_from=window.train_from,
                train_to=window.train_to,
                test_from=window.test_from,
                test_to=window.test_to,
                train_sweep_id=training.id,
            )
        )
        session.commit()

    # Cut per fold: a batch shares one window, and every fold trains on its own.
    for fold_runs in by_fold:
        await enqueue_runs(queue, jobs_for(session, fold_runs, batch=settings.tradeforge_batch))
    await queue.enqueue_job(RUN_SWEEP_WALK_FORWARD, str(walk.id), _defer_by=FIRST_LOOK)
    return CreatedSweepWalkForward(id=walk.id, folds=len(planned), runs=len(queued))


_EXCLUDED_SAYS: dict[str, str] = {
    Excluded.NO_COUNTS: "recorded before runs kept their trades by year",
    Excluded.REFUSED_CUT: "refused by the cut's guard",
    Excluded.UNDER_FLOOR: "under the trade floor",
    Excluded.POSITIVE_YEARS: "under the share of positive years",
}
"""How a fold's error names each reason a run was left out (`sweep_walkforward_cut.Excluded`)."""


def _years(years: list[int]) -> str:
    return ", ".join(str(year) for year in years)


def _check_cut(request: CreateSweepWalkForward, parent: Sweep, planned: list[Window]) -> None:
    """The 422s of a walk-forward by cut, before anything is kept: what a cut cannot measure, and
    years the parent does not hold whole.

    ⚠️ **A metric is refused only when asked for.** The request's default is the net profit, which
    a re-run ranks by; by cut, leaving it out means net R, and naming another is the 422.
    """
    if "metric" in request.model_fields_set and request.metric is not HoldoutRank.NET_R:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"a walk-forward by cut ranks by net R only, not {request.metric.value}: a cut "
            "keeps R and trades by year, and nothing else",
        )
    if request.max_drawdown_r is not None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="a walk-forward by cut has no drawdown limit: a cut keeps no drawdown in R",
        )
    outside, partial = uncovered_years(planned, parent.date_from, parent.date_to)
    window = f"{parent.date_from:%Y-%m-%d} to {parent.date_to:%Y-%m-%d}"
    if outside:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"the sweep ran {window}: {_years(outside)} lie outside it, and a cut answers "
            "only years the sweep ran",
        )
    if partial:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"the sweep ran {window}: {_years(partial)} are not whole years in it, and a "
            "cut answers whole years only",
        )


def _cut_runs(session: Session, parent: Sweep, charts: list[str] | None) -> list[CutRun]:
    """The parent's finished runs as the cut reads them, in launch order (`_runs_of`) — the
    candidates a reserved-window test of it would rank (`launch_holdout`)."""
    own, _followers = _points_of(session, parent)
    runs: list[CutRun] = []
    for order, (run, symbol) in enumerate(_runs_of(session, parent, done_only=True)):
        point = own.get(str(run.strategy_id))
        metrics = run.metrics
        if point is None or metrics is None:
            continue
        if charts is not None and run.timeframe not in charts:
            continue
        runs.append(
            CutRun(
                run_id=str(run.id),
                group=(str(point["entry_id"]), run.timeframe, symbol),
                order=order,
                label=f"{symbol} · {point['label']}",
                behaviour=behaviour(metrics),
                r_by_years=metrics.r_by_years,
                trades_by_years=metrics.trades_by_years,
                sizing_by_years=metrics.sizing_by_years,
                sizing_refusals=metrics.sizing_refusals,
                initial_capital=run.initial_capital,
                date_from=run.date_from,
                date_to=run.date_to,
            )
        )
    return runs


def _walk_by_cut(  # noqa: PLR0913, PLR0917 — the request, and what the endpoint read of it
    session: Session,
    parent: Sweep,
    request: CreateSweepWalkForward,
    planned: list[Window],
    charts: list[str] | None,
    prior: list[WindowUse],
) -> SweepWalkForward:
    """Keep a walk-forward by cut with every fold answered, `done` at once.

    ⚠️ **A fold with nothing to choose is recorded, not fatal** — as a fold that re-runs: its
    error names what was left out, and it is no look at its test years (`_fold_uses` skips it).
    """
    rule = request.model_dump(
        mode="json",
        include={"top_n", "min_trades", "min_positive_year_share", "distinct"},
        exclude_none=True,
    ) | {"metric": HoldoutRank.NET_R.value}
    rule |= retest_rule(prior)
    cut_rule = CutRule(
        top_n=request.top_n,
        floors={**RANK_MIN_TRADES, **request.min_trades},
        min_positive_year_share=request.min_positive_year_share,
        distinct=request.distinct,
    )
    runs = _cut_runs(session, parent, charts)
    walk = SweepWalkForward(
        parent_sweep_id=parent.id,
        start_year=request.start_year,
        train_years=request.train_years,
        test_years=request.test_years,
        anchored=request.anchored,
        mode="cut",
        rule=rule,
        status=BacktestStatus.DONE,
        finished_at=dt.datetime.now(tz=dt.UTC),
    )
    session.add(walk)
    session.flush()
    for index, window in enumerate(planned):
        found = fold_cut(runs, window, cut_rule)
        error = None
        if not found.picks:
            left = ", ".join(
                f"{count} {_EXCLUDED_SAYS.get(reason, reason)}"
                for reason, count in found.excluded.items()
            )
            error = (
                f"no run of the sweep can be ranked on "
                f"{window.train_from.year}-{window.train_to.year - 1}: "
                + (left or "it has no finished run on these charts")
            )
        session.add(
            SweepWalkForwardFold(
                walk_forward_id=walk.id,
                index=index,
                train_from=window.train_from,
                train_to=window.train_to,
                test_from=window.test_from,
                test_to=window.test_to,
                cut=fold_document(found),
                error=error,
            )
        )
    session.commit()
    return walk


@router.get("/sweeps/{sweep_id}/walkforwards", response_model=list[SweepWalkForwardOut])
def list_sweep_walk_forwards(sweep_id: uuid.UUID, session: SessionDep) -> list[SweepWalkForwardOut]:
    """Every walk-forward of this sweep, newest first."""
    rows = session.scalars(
        select(SweepWalkForward)
        .where(SweepWalkForward.parent_sweep_id == sweep_id)
        .order_by(SweepWalkForward.created_at.desc())
    )
    return [walk_forward_out(session, row) for row in rows]


@router.get(
    "/sweep-walkforwards/{walk_forward_id}",
    response_model=SweepWalkForwardOut,
    responses=_NOT_FOUND,
)
def get_sweep_walk_forward(walk_forward_id: uuid.UUID, session: SessionDep) -> SweepWalkForwardOut:
    walk = session.get(SweepWalkForward, walk_forward_id)
    if walk is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="walk-forward not found")
    return walk_forward_out(session, walk)


def _stage(session: SessionDep, fold: SweepWalkForwardFold) -> str:
    if fold.error is not None:
        return "failed"
    if fold.cut is not None:
        return "done"
    if fold.test_sweep_id is None:
        return "training"
    return "done" if settled(session, fold.test_sweep_id) else "testing"


def walk_forward_out(session: SessionDep, walk: SweepWalkForward) -> SweepWalkForwardOut:
    """The folds with their stage, and each (entry, chart) read across the folds' tests."""
    if walk.mode == "cut":
        return _cut_out(session, walk)
    per_group: dict[tuple[str, str], list[FoldGroup]] = {}
    for fold in walk.folds:
        if fold.test_sweep_id is None:
            continue
        test = get_holdout(fold.test_sweep_id, session)
        chosen: dict[tuple[str, str], list[str]] = {}
        for row in test.rows:
            chosen.setdefault((row.entry_id, row.timeframe), []).append(
                f"{row.symbol} · {row.label}"
            )
        for group in test.groups:
            key = (group.entry_id, group.timeframe)
            per_group.setdefault(key, []).append(
                FoldGroup(
                    fold=fold.index,
                    median_return=group.out_of_sample_median_return,
                    positive_share=group.out_of_sample_positive,
                    chosen=chosen.get(key, []),
                )
            )

    names = _names(session, {entry_id for entry_id, _timeframe in per_group})
    groups: list[SweepWalkForwardGroupOut] = []
    for (entry_id, timeframe), members in sorted(per_group.items()):
        by_fold: dict[int, Decimal | None] = {one.fold: one.median_return for one in members}
        held = stability(members)
        groups.append(
            SweepWalkForwardGroupOut(
                entry_id=entry_id,
                entry_name=names.get(entry_id),
                timeframe=timeframe,
                medians=[by_fold.get(fold.index) for fold in walk.folds],
                folds=held.folds,
                positive_folds=held.positive_folds,
                most_chosen=held.most_chosen,
                most_chosen_folds=held.most_chosen_folds,
            )
        )

    return _out(session, walk, groups)


def _names(session: Session, entry_ids: set[str]) -> dict[str, str]:
    return {
        str(entry.id): entry.name
        for entry in session.scalars(
            select(CatalogEntry).where(
                CatalogEntry.id.in_([uuid.UUID(one) for one in sorted(entry_ids) if one])
            )
        )
    }


def _decimal(value: object) -> Decimal | None:
    return None if value is None else Decimal(str(value))


def _cut_out(session: Session, walk: SweepWalkForward) -> SweepWalkForwardOut:
    """A walk-forward by cut, read from what its folds kept (`fold_document`): the groups a
    re-run's tests give — in R — with the in-sample median and the share beside each fold's."""
    per_group: dict[tuple[str, str], dict[int, dict[str, Any]]] = {}
    for fold in walk.folds:
        if fold.cut is None or fold.error is not None:
            continue
        for group in fold.cut.get("groups", []):
            key = (str(group["entry_id"]), str(group["timeframe"]))
            per_group.setdefault(key, {})[fold.index] = group
    names = _names(session, {entry_id for entry_id, _timeframe in per_group})
    indexes = [fold.index for fold in walk.folds]
    groups: list[SweepWalkForwardGroupOut] = []
    for (entry_id, timeframe), by_fold in sorted(per_group.items()):
        held = stability(
            [
                FoldGroup(
                    fold=index,
                    median_return=_decimal(group["out_of_sample_median_r"]),
                    positive_share=_decimal(group["out_of_sample_positive"]),
                    chosen=list(group["chosen"]),
                )
                for index, group in sorted(by_fold.items())
            ]
        )
        cells = [by_fold.get(index, {}) for index in indexes]
        groups.append(
            SweepWalkForwardGroupOut(
                entry_id=entry_id,
                entry_name=names.get(entry_id),
                timeframe=timeframe,
                medians=[_decimal(cell.get("out_of_sample_median_r")) for cell in cells],
                folds=held.folds,
                positive_folds=held.positive_folds,
                most_chosen=held.most_chosen,
                most_chosen_folds=held.most_chosen_folds,
                in_sample_medians=[_decimal(cell.get("in_sample_median_r")) for cell in cells],
                positive_shares=[_decimal(cell.get("out_of_sample_positive")) for cell in cells],
                no_trades_out=[cell.get("no_trades_out") for cell in cells],
            )
        )
    return _out(session, walk, groups)


def _out(
    session: Session, walk: SweepWalkForward, groups: list[SweepWalkForwardGroupOut]
) -> SweepWalkForwardOut:
    return SweepWalkForwardOut(
        id=walk.id,
        parent_sweep_id=walk.parent_sweep_id,
        start_year=walk.start_year,
        train_years=walk.train_years,
        test_years=walk.test_years,
        anchored=walk.anchored,
        mode="cut" if walk.mode == "cut" else "rerun",
        rule=dict(walk.rule),
        status=walk.status.value,
        error=walk.error,
        created_at=walk.created_at,
        finished_at=walk.finished_at,
        folds=[
            SweepWalkForwardFoldOut(
                index=fold.index,
                train_from=fold.train_from,
                train_to=fold.train_to,
                test_from=fold.test_from,
                test_to=fold.test_to,
                train_sweep_id=fold.train_sweep_id,
                test_sweep_id=fold.test_sweep_id,
                stage=_stage(session, fold),
                error=fold.error,
                candidates=None if fold.cut is None else fold.cut.get("candidates"),
                chosen=None if fold.cut is None else len(fold.cut.get("chosen", [])),
                excluded=None if fold.cut is None else dict(fold.cut.get("excluded", {})),
            )
            for fold in walk.folds
        ],
        groups=groups,
    )


__all__ = ["router", "walk_forward_out"]

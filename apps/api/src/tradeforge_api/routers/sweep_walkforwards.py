"""A sweep's walk-forward (25/09, path B): launched from a finished sweep, read back fold by fold.

Each fold's training is an ordinary sweep (`launch_window`) and each fold's test an ordinary
reserved-window test (`launch_holdout`), so they are queued, read, sliced and resampled by
everything those already have. This router ties them together and reads them as one answer.
"""

import datetime as dt
import uuid
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import select

from tradeforge_api.deps import QueueDep, SessionDep
from tradeforge_api.queue import RUN_BACKTEST, RUN_SWEEP_WALK_FORWARD
from tradeforge_api.routers.sweeps import get_holdout, launch_window
from tradeforge_api.schemas import (
    CreatedSweepWalkForward,
    CreateSweepWalkForward,
    SweepWalkForwardFoldOut,
    SweepWalkForwardGroupOut,
    SweepWalkForwardOut,
)
from tradeforge_api.sweep_walkforward import FoldGroup, stability, windows
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
    sweep_id: uuid.UUID, request: CreateSweepWalkForward, session: SessionDep, queue: QueueDep
) -> CreatedSweepWalkForward:
    """Keep the walk-forward, launch every fold's training at once, and start the conductor.

    ⚠️ **Refused for a reserved-window test** — its points were chosen elsewhere, and walking it
    forward would re-choose among a handful — and for windows whose first test starts in the
    future, which would test nothing.
    """
    parent = session.get(Sweep, sweep_id)
    if parent is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="sweep not found")
    if parent.holdout_rule is not None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="a reserved-window test cannot walk forward; walk the sweep it came from",
        )
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
    if planned[0].test_from >= dt.datetime.now(tz=dt.UTC):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"the first test starts {planned[0].test_from:%Y-%m-%d}, in the future",
        )

    rule = request.model_dump(
        mode="json",
        include={"top_n", "metric", "min_trades", "max_drawdown_r", "min_positive_year_share"},
        exclude_none=True,
    )
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
    for index, window in enumerate(planned):
        training, runs = launch_window(session, parent, window.train_from, window.train_to)
        queued += runs
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

    for run in queued:
        await queue.enqueue_job(RUN_BACKTEST, str(run.id), _job_id=str(run.id))
    await queue.enqueue_job(RUN_SWEEP_WALK_FORWARD, str(walk.id), _defer_by=FIRST_LOOK)
    return CreatedSweepWalkForward(id=walk.id, folds=len(planned), runs=len(queued))


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
    if fold.test_sweep_id is None:
        return "training"
    return "done" if settled(session, fold.test_sweep_id) else "testing"


def walk_forward_out(session: SessionDep, walk: SweepWalkForward) -> SweepWalkForwardOut:
    """The folds with their stage, and each (entry, chart) read across the folds' tests."""
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

    entry_ids = sorted({entry_id for entry_id, _timeframe in per_group})
    names = {
        str(entry.id): entry.name
        for entry in session.scalars(
            select(CatalogEntry).where(
                CatalogEntry.id.in_([uuid.UUID(one) for one in entry_ids if one])
            )
        )
    }
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

    return SweepWalkForwardOut(
        id=walk.id,
        parent_sweep_id=walk.parent_sweep_id,
        start_year=walk.start_year,
        train_years=walk.train_years,
        test_years=walk.test_years,
        anchored=walk.anchored,
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
            )
            for fold in walk.folds
        ],
        groups=groups,
    )


__all__ = ["router", "walk_forward_out"]

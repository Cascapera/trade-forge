"""The walk-forward's conductor: one step at a time, called again until every fold is tested.

Every training sweep is queued at launch — the folds are independent, so the workers run them
side by side. What cannot start at launch is a fold's test: its points are chosen from its
training, so it waits for that training to end. Each call looks at every fold, launches the tests
whose training has ended, and says whether anything is still to come.
"""

import datetime as dt
import uuid

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from tradeforge_api.routers.sweeps import launch_holdout
from tradeforge_api.schemas import CreateHoldout
from tradeforge_db.models import Backtest, BacktestStatus, SweepWalkForward

_OPEN = (BacktestStatus.QUEUED, BacktestStatus.RUNNING)


def _now() -> dt.datetime:
    return dt.datetime.now(tz=dt.UTC)


def settled(session: Session, sweep_id: uuid.UUID) -> bool:
    """Nothing of this sweep is still queued or running."""
    return (
        session.scalar(
            select(func.count())
            .select_from(Backtest)
            .where(Backtest.sweep_id == sweep_id, Backtest.status.in_(_OPEN))
        )
        == 0
    )


def advance(session: Session, walk_forward_id: uuid.UUID) -> tuple[list[Backtest], bool]:
    """Launch every test whose training has ended; the runs to queue, and whether to call again.

    ⚠️ **A fold that cannot be tested is recorded, not fatal.** Nothing in its training passing
    the floors, or no candles in its test window, is that fold's answer — the other folds go on,
    and the walk-forward ends `done` with that fold's reason beside it.
    """
    walk = session.get(SweepWalkForward, walk_forward_id)
    if walk is None or walk.status in (BacktestStatus.DONE, BacktestStatus.FAILED):
        return [], False
    walk.status = BacktestStatus.RUNNING
    session.commit()

    queue: list[Backtest] = []
    for fold in walk.folds:
        if fold.error is not None or fold.test_sweep_id is not None:
            continue
        if fold.train_sweep_id is None:
            fold.error = "its training sweep was deleted"
            continue
        if not settled(session, fold.train_sweep_id):
            continue
        # A test window reaching past today ends today: there is nothing later to test on.
        until = min(fold.test_to, _now())
        try:
            test, runs, _skipped = launch_holdout(
                session,
                fold.train_sweep_id,
                CreateHoldout.model_validate(
                    {**walk.rule, "date_from": fold.test_from, "date_to": until}
                ),
            )
        except HTTPException as refused:
            session.rollback()
            fold.error = str(refused.detail)
        else:
            fold.test_sweep_id = test.id
            queue += runs
        session.commit()

    pending = any(
        fold.error is None
        and (fold.test_sweep_id is None or not settled(session, fold.test_sweep_id))
        for fold in walk.folds
    )
    if not pending:
        walk.status = BacktestStatus.DONE
        walk.finished_at = _now()
        session.commit()
    return queue, pending


__all__ = ["advance", "settled"]

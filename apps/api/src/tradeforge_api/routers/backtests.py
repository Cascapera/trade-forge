"""`/backtests` — enqueue a run, then read its status, metrics, trades and equity curve.

`POST` does the least it possibly can: validate the request, write a `queued` row, drop a job
on the queue, and return `202`. It never touches the engine — that is the whole point of the
worker (a ten-year backtest in the request path would block the event loop and starve every
other caller). The GETs read the state the worker writes back.
"""

import uuid
from typing import Any

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import func, select

from tradeforge_api.deps import QueueDep, SessionDep
from tradeforge_api.queue import RUN_BACKTEST
from tradeforge_api.runner import ENGINE_VERSION
from tradeforge_api.schemas import (
    BacktestOut,
    CreateBacktestRequest,
    CreatedBacktest,
    EquityPointOut,
    TradeOut,
    TradesPage,
)
from tradeforge_collector import step
from tradeforge_db.models import Backtest, BacktestStatus, Instrument, Strategy, Trade

router = APIRouter(tags=["backtests"])

# Declared so the OpenAPI schema documents the 404 these paths can return (on POST it is a
# missing strategy; on the reads, a missing backtest) — kept honest by the schemathesis test.
_Responses = dict[int | str, dict[str, Any]]
_NOT_FOUND: _Responses = {status.HTTP_404_NOT_FOUND: {"description": "not found"}}
# FastAPI answers an unparseable JSON body with 400, before validation ever runs.
_BAD_BODY: _Responses = {status.HTTP_400_BAD_REQUEST: {"description": "malformed request body"}}


def _load(session: SessionDep, backtest_id: uuid.UUID) -> Backtest:
    backtest = session.get(Backtest, backtest_id)
    if backtest is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="backtest not found")
    return backtest


@router.post(
    "/backtests",
    response_model=CreatedBacktest,
    status_code=status.HTTP_202_ACCEPTED,
    responses={**_NOT_FOUND, **_BAD_BODY},
)
async def create_backtest(
    request: CreateBacktestRequest, session: SessionDep, queue: QueueDep
) -> Backtest:
    """Validate against the current data, persist a queued run, and enqueue it.

    The strategy and instrument must exist and the timeframe must be one the DSL knows —
    caught here so the worker never picks up a job that cannot possibly run. `engine_version`
    is stamped now: reproducing this result later needs both the strategy *and* the engine
    that executed it.
    """
    strategy = session.get(Strategy, request.strategy_id)
    if strategy is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="strategy not found")

    instrument = session.scalars(
        select(Instrument).where(Instrument.symbol == request.symbol)
    ).first()
    if instrument is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"unknown symbol {request.symbol!r}",
        )

    try:
        step(request.timeframe)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc

    if request.date_to < request.date_from:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="date_to precedes date_from"
        )

    backtest = Backtest(
        strategy_id=strategy.id,
        instrument_id=instrument.id,
        timeframe=request.timeframe,
        date_from=request.date_from,
        date_to=request.date_to,
        initial_capital=request.initial_capital,
        cost_model=request.cost_model,
        status=BacktestStatus.QUEUED,
        engine_version=ENGINE_VERSION,
    )
    session.add(backtest)
    session.commit()
    session.refresh(backtest)

    await queue.enqueue_job(RUN_BACKTEST, str(backtest.id))
    return backtest


@router.get("/backtests/{backtest_id}", response_model=BacktestOut, responses=_NOT_FOUND)
def get_backtest(backtest_id: uuid.UUID, session: SessionDep) -> Backtest:
    """The run and, once it has finished, its metrics."""
    return _load(session, backtest_id)


@router.get("/backtests/{backtest_id}/trades", response_model=TradesPage, responses=_NOT_FOUND)
def list_trades(
    backtest_id: uuid.UUID,
    session: SessionDep,
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
) -> TradesPage:
    """Trades in entry order, paginated. `total` is returned so a client can size its pager."""
    _load(session, backtest_id)
    total = session.scalar(
        select(func.count()).select_from(Trade).where(Trade.backtest_id == backtest_id)
    )
    rows = session.scalars(
        select(Trade)
        .where(Trade.backtest_id == backtest_id)
        .order_by(Trade.id)
        .limit(limit)
        .offset(offset)
    ).all()
    return TradesPage(
        total=total or 0,
        limit=limit,
        offset=offset,
        items=[TradeOut.model_validate(row) for row in rows],
    )


@router.get(
    "/backtests/{backtest_id}/equity",
    response_model=list[EquityPointOut],
    responses=_NOT_FOUND,
)
def get_equity(backtest_id: uuid.UUID, session: SessionDep) -> list[EquityPointOut]:
    """The equity curve, once the run has finished. 404 while there are no results yet."""
    backtest = _load(session, backtest_id)
    if backtest.metrics is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="backtest has no results yet"
        )
    return [EquityPointOut.model_validate(point) for point in backtest.metrics.equity_curve]

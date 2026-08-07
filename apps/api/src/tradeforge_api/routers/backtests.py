"""`/backtests` — enqueue a run, then read its status, metrics, trades and equity curve.

`POST` does the least it possibly can: validate the request, write a `queued` row, drop a job
on the queue, and return `202`. It never touches the engine — that is the whole point of the
worker (a ten-year backtest in the request path would block the event loop and starve every
other caller). The GETs read the state the worker writes back.
"""

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from tradeforge_api.deps import QueueDep, SessionDep
from tradeforge_api.queue import RUN_BACKTEST
from tradeforge_api.runner import ENGINE_VERSION
from tradeforge_api.schemas import (
    BacktestListItem,
    BacktestOut,
    BacktestsPage,
    CreateBacktestRequest,
    CreatedBacktest,
    EquityPointOut,
    MetricsOut,
    SnapshotOut,
    TradeOut,
    TradesPage,
)
from tradeforge_collector import step
from tradeforge_db.models import (
    Backtest,
    BacktestMetrics,
    BacktestStatus,
    Instrument,
    Strategy,
    Trade,
)

router = APIRouter(tags=["backtests"])

# Declared so the OpenAPI schema documents the 404 these paths can return (on POST it is a
# missing strategy; on the reads, a missing backtest) — kept honest by the schemathesis test.
_Responses = dict[int | str, dict[str, Any]]
_NOT_FOUND: _Responses = {status.HTTP_404_NOT_FOUND: {"description": "not found"}}
# FastAPI answers an unparseable JSON body with 400, before validation ever runs.
_BAD_BODY: _Responses = {status.HTTP_400_BAD_REQUEST: {"description": "malformed request body"}}

# The largest `offset` the database can be asked for. Postgres renders OFFSET as a bigint, so a
# larger number is not a big page — it is a `NumericValueOutOfRange` raised inside the driver,
# which surfaces as a 500 on input a client fully controls. Bounding it here turns that into the
# 422 it always was: a request outside what the parameter can mean.
#
# The bound is the type's own limit rather than a guess at a sensible page depth. Every value it
# admits is genuinely valid SQL that returns an empty page, so nothing legitimate is refused, and
# there is no invented business number to be wrong about later.
_MAX_OFFSET = 9_223_372_036_854_775_807  # 2**63 - 1, Postgres bigint


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


@router.get("/backtests", response_model=BacktestsPage)
def list_backtests(  # noqa: PLR0913 — one filter per column a run is chosen by
    session: SessionDep,
    # `Annotated` rather than a `Query(...)` default: the call then lives in the annotation
    # instead of the default value, which is what FastAPI now recommends and what keeps the
    # status filter out of B008. It has to be spelled `status` on the wire and cannot be named
    # that here, because `fastapi.status` is already in scope for the response codes.
    symbol: Annotated[str | None, Query(description="exact instrument symbol, e.g. EURUSD")] = None,
    timeframe: Annotated[str | None, Query(description="exact timeframe, e.g. H1")] = None,
    run_status: Annotated[BacktestStatus | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0, le=_MAX_OFFSET)] = 0,
) -> BacktestsPage:
    """Every run, newest first, with the metrics that decide which one is worth opening.

    Newest first because this is a research log: the run you want is nearly always one you just
    launched, and paging backwards to reach it would be the wrong default for the only reader
    this endpoint has.

    **The equity curve is deferred, and that is the whole performance story here.** It is a JSONB
    column on the metrics row, and it is large — measured on this project's own database, 33
    finished runs carry 9 MB of curve between them, the largest 856 kB on its own. It never
    reaches the response, because `MetricsOut` has no field for it; the cost is entirely in what
    Postgres ships to the application to build rows nothing will read. Measured over those 36
    rows:

    * lazy loading, the default — **37 queries, 145 ms**. One query per row, the N+1 that any
      relationship touched in a loop produces.
    * eager loading alone — 2 queries, 81 ms. The N+1 is gone and the 9 MB still crosses.
    * eager loading with the curve deferred — 2 queries, **4.8 ms**.

    Thirty times the naive version, and the gap widens with every run added, since the curve grows
    with the length of the backtest rather than with the number of them. A client that wants a
    curve asks for one run's, from `/backtests/{id}/equity`.

    The joins to instrument and strategy are inner joins on non-nullable foreign keys with
    `ondelete="RESTRICT"`, so they cannot drop a row: a backtest whose instrument or strategy had
    vanished could not have been written in the first place.
    """
    filters = []
    if symbol is not None:
        filters.append(Instrument.symbol == symbol)
    if timeframe is not None:
        filters.append(Backtest.timeframe == timeframe)
    if run_status is not None:
        filters.append(Backtest.status == run_status)

    base = (
        select(Backtest, Instrument.symbol, Strategy.name, Strategy.version)
        .join(Instrument, Instrument.id == Backtest.instrument_id)
        .join(Strategy, Strategy.id == Backtest.strategy_id)
        .where(*filters)
    )
    total = session.scalar(
        select(func.count())
        .select_from(Backtest)
        .join(Instrument, Instrument.id == Backtest.instrument_id)
        .join(Strategy, Strategy.id == Backtest.strategy_id)
        .where(*filters)
    )
    rows = session.execute(
        base.options(selectinload(Backtest.metrics).defer(BacktestMetrics.equity_curve))
        .order_by(Backtest.created_at.desc(), Backtest.id)
        .limit(limit)
        .offset(offset)
    ).all()

    return BacktestsPage(
        total=total or 0,
        limit=limit,
        offset=offset,
        items=[
            BacktestListItem(
                id=run.id,
                strategy_id=run.strategy_id,
                strategy_name=name,
                strategy_version=version,
                symbol=run_symbol,
                timeframe=run.timeframe,
                date_from=run.date_from,
                date_to=run.date_to,
                initial_capital=run.initial_capital,
                cost_model=run.cost_model,
                status=run.status,
                error=run.error,
                created_at=run.created_at,
                finished_at=run.finished_at,
                metrics=(None if run.metrics is None else MetricsOut.model_validate(run.metrics)),
            )
            for run, run_symbol, name, version in rows
        ],
    )


@router.get("/backtests/{backtest_id}", response_model=BacktestOut, responses=_NOT_FOUND)
def get_backtest(backtest_id: uuid.UUID, session: SessionDep) -> Backtest:
    """The run and, once it has finished, its metrics."""
    return _load(session, backtest_id)


@router.get("/backtests/{backtest_id}/trades", response_model=TradesPage, responses=_NOT_FOUND)
def list_trades(
    backtest_id: uuid.UUID,
    session: SessionDep,
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0, le=_MAX_OFFSET),
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
    "/backtests/{backtest_id}/trades/{trade_id}/snapshot",
    response_model=SnapshotOut,
    responses=_NOT_FOUND,
)
def get_trade_snapshot(
    backtest_id: uuid.UUID, trade_id: int, session: SessionDep
) -> dict[str, Any]:
    """The bars the strategy was looking at when it entered this trade.

    Served one at a time on purpose. The window is fifty-odd bars, and a reader opens the two
    or three entries that look wrong out of a run of hundreds — see `TradeOut.has_snapshot`.

    Scoped by backtest rather than looked up by trade id alone. Trade ids are globally unique,
    so the join is not needed to *find* the row; it is here so that a wrong backtest in the URL
    is a 404 instead of quietly serving a chart belonging to a different run.
    """
    _load(session, backtest_id)
    trade = session.scalar(
        select(Trade).where(Trade.id == trade_id, Trade.backtest_id == backtest_id)
    )
    if trade is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="trade not found")
    # `{}` is the column's NOT NULL default: a run older than `rev_0003`, or a strategy driven
    # without a window. Distinguished from a missing trade, because the two are different
    # questions and a client that conflated them would show "not found" for a real trade.
    if not trade.snapshot:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="this trade recorded no entry snapshot",
        )
    return trade.snapshot


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

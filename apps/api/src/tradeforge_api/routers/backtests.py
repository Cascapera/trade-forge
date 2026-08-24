"""`/backtests` — enqueue a run, then read its status, metrics, trades and equity curve.

`POST` does the least it possibly can: validate the request, write a `queued` row, drop a job
on the queue, and return `202`. It never touches the engine — that is the whole point of the
worker (a ten-year backtest in the request path would block the event loop and starve every
other caller). The GETs read the state the worker writes back.
"""

import datetime as dt
import uuid
from dataclasses import dataclass
from decimal import Decimal, localcontext
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from tradeforge_api.config import Settings
from tradeforge_api.deps import QueueDep, SessionDep, SettingsDep
from tradeforge_api.queue import RUN_BACKTEST
from tradeforge_api.runner import ENGINE_VERSION, instrument_spec
from tradeforge_api.schemas import (
    BacktestListItem,
    BacktestOut,
    BacktestsPage,
    CandleOut,
    CandlesOut,
    CreateBacktestRequest,
    CreatedBacktest,
    EquityPointOut,
    MetricsOut,
    OverlaySeriesOut,
    OverlaysOut,
    SnapshotOut,
    StorableText,
    Symbol,
    TradeOut,
    TradesPage,
    ZoneOut,
)
from tradeforge_collector import read_candles, step
from tradeforge_db.models import (
    Backtest,
    BacktestMetrics,
    BacktestStatus,
    Instrument,
    Strategy,
    Trade,
)
from tradeforge_engine.domain import AccountState, Candle, Context
from tradeforge_engine.loop import ENGINE_CONTEXT
from tradeforge_engine.protocols import Charted, Zoned
from tradeforge_engine.strategy import compile_strategy

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

# How many bars `/candles` will put in one response.
#
# A cap and not a reduction, which is the decision worth recording. The tempting alternative
# is to decimate — serve every tenth bar of an oversized run — and it is wrong in a way that
# looks right on screen. A candle is not a sample of a price; it is the *summary of an
# interval*. Dropping nine of ten throws away highs and lows, and the high thrown away may be
# precisely the one that hit a stop. The chart would be smooth, plausible, and would no longer
# contain the bar that explains the trade beside it.
#
# The correct reduction is aggregation — first open, highest high, lowest low, last close —
# which preserves the extremes but is a real rule with real edges (what a partial bucket at
# the end of the window means) and deserves its own tests. It is not built here because there
# is nothing to build it for yet: the largest dataset this project holds is 38 986 M15 bars,
# and the longest run ever executed read 12 883. Everything collected fits whole, so nothing
# is reduced, and the day an M1 series is collected the aggregating slice has a caller.
_MAX_CANDLES = 50_000
_TOO_MANY_CANDLES: _Responses = {
    status.HTTP_422_UNPROCESSABLE_CONTENT: {"description": "run too long to chart in one response"}
}


def list_item(
    run: Backtest, symbol: str, strategy_name: str, strategy_version: int
) -> BacktestListItem:
    """Build one run-log row from a run and the three fields its foreign keys resolve to.

    Shared with `/baskets/{id}`, which reports the same rows for the runs it groups. A basket
    read that assembled its own would drift from this one the first time a column is added —
    and the two views would then disagree about what a run *is* while both looking right.
    """
    return BacktestListItem(
        id=run.id,
        strategy_id=run.strategy_id,
        strategy_name=strategy_name,
        strategy_version=strategy_version,
        symbol=symbol,
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


def _load(session: SessionDep, backtest_id: uuid.UUID) -> Backtest:
    backtest = session.get(Backtest, backtest_id)
    if backtest is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="backtest not found")
    return backtest


@dataclass(frozen=True, slots=True)
class _Window:
    """A run's bars plus everything the two chart endpoints need to describe them."""

    backtest: Backtest
    instrument: Instrument
    strategy: Strategy
    seen: int
    first: dt.datetime
    last: dt.datetime
    candles: list[Candle]


def _read_the_window(session: SessionDep, settings: Settings, backtest: Backtest) -> _Window:
    """Load exactly the bars a finished run read, or refuse.

    Shared by `/candles` and `/overlays` because the two must never disagree about which bars
    the run saw — a curve computed over a different window than the candles under it would be
    drawn point by point against the wrong bars, and would still look like a curve.
    """
    first, last = backtest.first_candle, backtest.last_candle
    seen = backtest.candles_seen
    # The three are written together and constrained together (`candles_provenance` on the
    # table), so this is one condition, not three. Narrowing all three keeps the types honest.
    if seen is None or first is None or last is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="this run did not record which candles it read",
        )
    # Checked before touching the disk: `candles_seen` already answers "how big is this",
    # so an oversized run costs a row read rather than a scan that ends in a refusal.
    if seen > _MAX_CANDLES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=(
                f"this run read {seen} candles, over the {_MAX_CANDLES} this endpoint serves"
                " in one response"
            ),
        )

    instrument = session.get(Instrument, backtest.instrument_id)
    strategy = session.get(Strategy, backtest.strategy_id)
    if instrument is None or strategy is None:  # pragma: no cover — RESTRICT makes this unreachable
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="run references nothing")

    return _Window(
        backtest=backtest,
        instrument=instrument,
        strategy=strategy,
        seen=seen,
        first=first,
        last=last,
        candles=read_candles(
            settings.parquet_root,
            instrument.symbol,
            backtest.timeframe,
            start=first,
            end=last,
        ),
    )


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
    # Keyword-only from here. FastAPI fills these by name and nothing else calls the
    # endpoint at all, so the `*` costs nothing and removes the question a reader would
    # otherwise have to ask about argument order.
    *,
    # `Annotated` rather than a `Query(...)` default: the call then lives in the annotation
    # instead of the default value, which is what FastAPI now recommends and what keeps the
    # status filter out of B008. It has to be spelled `status` on the wire and cannot be named
    # that here, because `fastapi.status` is already in scope for the response codes.
    symbol: Annotated[
        Symbol | None, Query(description="exact instrument symbol, e.g. EURUSD")
    ] = None,
    timeframe: Annotated[StorableText | None, Query(description="exact timeframe, e.g. H1")] = None,
    run_status: Annotated[BacktestStatus | None, Query(alias="status")] = None,
    include_generated: Annotated[
        bool, Query(description="include the runs a grid generated (hidden by default)")
    ] = False,
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
    if not include_generated:
        # ⚠️ **The runs a grid generated are hidden by default**, mirroring what
        # `GET /strategies` does with the strategies a grid generated, and for the same reason:
        # this is a research log, and one study of a hundred points buries every run made by
        # hand under a wall of `MME9 [period=17]`. A walk-forward makes that fatal rather than
        # annoying — six folds over a fifty-point grid is three hundred rows in one request.
        #
        # Derived, not marked: a generated run is one that belongs to a study. A `generated`
        # column would be a second place for the same truth, free to disagree with the first.
        #
        # ⚠️ **A walk-forward's out-of-sample runs stay visible, and that is the point of
        # drawing the line here.** They carry no `study_id` because nothing searched them —
        # they are the one run per fold that a *decision* produced, and they are exactly what a
        # reader scanning this log wants to find.
        filters.append(Backtest.study_id.is_(None))

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
            list_item(run, run_symbol, name, version) for run, run_symbol, name, version in rows
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


@router.get(
    "/backtests/{backtest_id}/candles",
    response_model=CandlesOut,
    responses={**_NOT_FOUND, **_TOO_MANY_CANDLES},
)
def get_candles(backtest_id: uuid.UUID, session: SessionDep, settings: SettingsDep) -> CandlesOut:
    """The price the run was executed over, for charting the trades against it.

    **Hung off the run, not off the instrument.** The obvious alternative was a general
    `/instruments/{symbol}/candles?timeframe&from&to`, and it is a superset of this — but it
    would make the *client* assemble the window, out of a symbol, a timeframe and two
    timestamps it holds separately. A client that assembles it slightly wrong gets a chart
    that disagrees with the trades drawn on it, and nothing about the picture would say so.
    Here the window is not a parameter at all: it is the provenance the run recorded, so the
    chart cannot be asked for a period the run did not execute over. The general endpoint can
    exist the day a second caller needs one.

    **The window comes from `first_candle`/`last_candle`, never from `date_from`/`date_to`.**
    Those are a *request*; the run may well have covered less (see the columns' own comment in
    `tradeforge_db.models`). Charting the requested period would draw bars beside trades that
    were never given the chance to happen on them.

    A run that recorded no provenance is refused rather than approximated. That is every
    `failed` run — it never reached a candle — and any row written before `rev_0002`. Falling
    back to the requested dates there would produce exactly the quiet disagreement this
    endpoint is shaped to prevent.
    """
    read = _read_the_window(session, settings, _load(session, backtest_id))
    return CandlesOut(
        timeframe=read.backtest.timeframe,
        symbol=read.instrument.symbol,
        candles_seen=read.seen,
        first_candle=read.first,
        last_candle=read.last,
        count=len(read.candles),
        candles=[CandleOut.model_validate(candle) for candle in read.candles],
    )


def _zones_of(read: _Window) -> list[ZoneOut]:
    """Replay the run's bars through the strategy and collect the regions it marked.

    **This one has to run `on_bar`, unlike the curves.** An indicator can be driven on its own
    because it folds a candle and nothing else; a region is marked by a detector that needs the
    structure break the *same* bar produced, and the strategy is what wires those two together.
    Driving the detector directly from here would put that wiring in a second place — the exact
    duplication `Charted` exists to avoid.

    Replaying with a flat account is faithful for the same reason the standalone average is: the
    structure and the detector are advanced on the **first two lines** of `on_bar`, before any
    branch that reads a position. What a position changes is which zone gets an order, and no
    order is placed here — the signals are discarded and the broker never exists.

    The account is a plausible stand-in rather than the run's real equity, and it cannot matter:
    nothing on the marking path reads it. Sizing does, and sizing is the risk manager's, which
    is not in this loop at all.

    ⚠️ **It compiles its own strategy rather than sharing the caller's.** The curve loop has
    already driven that one over the whole window, and a strategy that was both `Charted` and
    `Zoned` would arrive here with its indicators a full window ahead of the bars about to be
    replayed — so the `series` served would no longer be the state the strategy held bar by bar.
    No strategy is both today, which is exactly why the day one becomes both is the day nobody
    would think to look here. One `compile_strategy` is the whole prevention.
    """
    strategy = compile_strategy(read.strategy.definition)
    # Two views of one object, and they are kept apart on purpose: `Zoned` declares only
    # `zones()`, so narrowing to it would take `on_bar` away — and this is the one reader that
    # needs both. The protocol stays minimal rather than growing a method it does not mean.
    marking = strategy if isinstance(strategy, Zoned) else None
    if marking is None:
        return []

    spec = instrument_spec(read.instrument)
    account = AccountState(
        equity=read.backtest.initial_capital,
        balance=read.backtest.initial_capital,
        currency=read.instrument.currency_quote,
    )
    with localcontext(ENGINE_CONTEXT):
        for candle in read.candles:
            strategy.on_bar(Context(candle=candle, instrument=spec, account=account))
        return [
            ZoneOut(
                kind=zone.kind,
                top=zone.top,
                bottom=zone.bottom,
                from_time=zone.from_time,
                confirmed_at=zone.confirmed_at,
                mitigated_at=zone.mitigated_at,
                primary=zone.primary,
            )
            for zone in marking.zones()
        ]


@router.get(
    "/backtests/{backtest_id}/overlays",
    response_model=OverlaysOut,
    responses={**_NOT_FOUND, **_TOO_MANY_CANDLES},
)
def get_overlays(backtest_id: uuid.UUID, session: SessionDep, settings: SettingsDep) -> OverlaysOut:
    """The curves the strategy was reading, bar by bar, over the window the run read.

    **Recomputed, not stored.** An indicator series is derivable from the candles and the
    parameters, and this project stores measurements rather than derivations — one run's equity
    curve already reaches 856 kB, and a series per indicator per run would grow the same way for
    something a chart reads once. The engine is deterministic, so recomputing returns what the
    run computed rather than something like it.

    **The strategy is asked what it was looking at; nothing here reads the document.** Reading
    `definition["indicators"]` was the obvious route and it is wrong twice over. It finds nothing
    at all for a `setup` strategy — every strategy in this project's own database is one, and the
    average they trade lives inside the setup, not in that block. And where it does find
    something, it puts "which average does this setup use" in a second place: the day a setup
    changed its average, a chart rebuilt from the document would keep drawing the old one,
    correct-looking and wrong. `Charted.overlays` hands back the strategy's own indicators, so
    there is no second place to disagree.

    A strategy that implements nothing charts as bars and trades, with an empty `series`.
    """
    read = _read_the_window(session, settings, _load(session, backtest_id))
    strategy = compile_strategy(read.strategy.definition)
    # Built for this request and used for nothing else. `overlays` hands back live, mutable
    # indicators (see `protocols.Charted`), so driving them advances the very state a running
    # strategy would read — safe here only because this instance never executes anything.
    overlays = strategy.overlays() if isinstance(strategy, Charted) else {}

    points: dict[str, list[tuple[dt.datetime, Decimal]]] = {label: [] for label in overlays}
    # ⚠️ Under the engine's own decimal context, not the request thread's. Today the two agree
    # byte for byte — CPython's default is `prec=28`/`ROUND_HALF_EVEN`, which is what
    # `ENGINE_CONTEXT` asks for — so this changes no number that exists now. It is here because
    # that agreement is a coincidence and not a guarantee: `getcontext()` is global and mutable
    # by anything in the process, and `ENGINE_CONTEXT` exists precisely to stop a run's
    # arithmetic depending on who touched it last. Measured at `prec=10` the curve comes back
    # `1.114858523` against the run's `1.114858524264470690451361286` — a chart disagreeing with
    # the trades on it in the ninth decimal, which no reader would ever see.
    with localcontext(ENGINE_CONTEXT):
        for candle in read.candles:
            for label, indicator in overlays.items():
                indicator.update(candle)
                value = indicator.value()
                # Warm-up bars are left out rather than sent as nulls, matching the entry
                # snapshot's own series: the curve simply begins where the indicator did. A null
                # per bar would have every client decide again what a hole means.
                if value is not None:
                    points[label].append((candle.time, value))

    return OverlaysOut(
        symbol=read.instrument.symbol,
        timeframe=read.backtest.timeframe,
        candles_seen=read.seen,
        count=len(read.candles),
        series=[OverlaySeriesOut(label=label, points=found) for label, found in points.items()],
        zones=_zones_of(read),
    )

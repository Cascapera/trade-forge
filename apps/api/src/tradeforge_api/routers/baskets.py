"""`/baskets` — one strategy over several markets, to see whether it travels.

A strategy tuned on one instrument's history and never tried elsewhere is indistinguishable
from a strategy fitted to that instrument's history. A basket is the experiment that tells
them apart: same strategy, same window, same capital, N symbols, results side by side.

`POST` does exactly what `POST /backtests` does, N times, in one transaction — it never runs
anything itself. Each symbol becomes its own `backtests` row and its own queued job, because
the engine holds one instrument and one position per run by construction. A shared-capital
portfolio, where positions compete for one account, is a different product; see the `Basket`
model for why it is not this one.
"""

import uuid
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from tradeforge_api.deps import QueueDep, SessionDep
from tradeforge_api.queue import RUN_BACKTEST
from tradeforge_api.routers.backtests import list_item
from tradeforge_api.runner import ENGINE_VERSION
from tradeforge_api.schemas import (
    BasketAggregate,
    BasketOut,
    BasketRunOut,
    CreateBasketRequest,
    CreatedBasket,
)
from tradeforge_collector import step
from tradeforge_db.models import (
    Backtest,
    BacktestMetrics,
    BacktestStatus,
    Basket,
    Instrument,
    Strategy,
)

router = APIRouter(tags=["baskets"])

_Responses = dict[int | str, dict[str, Any]]
_NOT_FOUND: _Responses = {status.HTTP_404_NOT_FOUND: {"description": "not found"}}
_BAD_BODY: _Responses = {status.HTTP_400_BAD_REQUEST: {"description": "malformed request body"}}


def _cost_model_for(instrument: Instrument) -> dict[str, Any]:
    """Charge this instrument its own measured spread, or nothing if nobody measured it.

    The spread crosses into the stored document as a **string**, not a float. It is a
    `Decimal` in the database and a `Decimal` in the engine, and JSON has only doubles: a
    number here would be the one place in the chain where an exact tick count becomes binary
    floating point, silently, inside a column that later reproduces the run.

    ⚠️ NULL becomes `{"type": "none"}` and never `spread_points: 0`. Nobody has measured the
    symbol; zero would be the claim that it is free to trade. The response reports which
    symbols this happened to, so a basket cannot quietly mix costed and uncosted runs and
    present them as one comparison.
    """
    if instrument.default_spread_points is None:
        return {"type": "none"}
    return {"type": "spread", "spread_points": str(instrument.default_spread_points)}


@router.post(
    "/baskets",
    response_model=CreatedBasket,
    status_code=status.HTTP_202_ACCEPTED,
    responses={**_NOT_FOUND, **_BAD_BODY},
)
async def create_basket(
    request: CreateBasketRequest, session: SessionDep, queue: QueueDep
) -> CreatedBasket:
    """Validate once, write the basket and its runs in one transaction, then enqueue.

    **Every symbol is resolved before anything is written.** A basket that half-exists is
    worse than one that was refused: the caller asked one question about four markets, and
    three runs plus an error answers a question nobody asked. So the unknown symbols are
    collected and reported together — naming all of them, not just the first, because a
    caller fixing a typo list one round trip at a time is a caller the API is failing.
    """
    strategy = session.get(Strategy, request.strategy_id)
    if strategy is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="strategy not found")

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

    found = {
        instrument.symbol: instrument
        for instrument in session.scalars(
            select(Instrument).where(Instrument.symbol.in_(request.symbols))
        )
    }
    unknown = [symbol for symbol in request.symbols if symbol not in found]
    if unknown:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"unknown symbols: {', '.join(unknown)}",
        )

    basket = Basket(
        strategy_id=strategy.id,
        timeframe=request.timeframe,
        date_from=request.date_from,
        date_to=request.date_to,
        initial_capital=request.initial_capital,
    )
    session.add(basket)

    # Ordered by the request, not by the database: the caller listed the symbols in some order
    # and gets them back in it. Set iteration order would be arbitrary and would change between
    # otherwise identical calls.
    runs = [
        Backtest(
            basket=basket,
            strategy_id=strategy.id,
            instrument_id=found[symbol].id,
            timeframe=request.timeframe,
            date_from=request.date_from,
            date_to=request.date_to,
            initial_capital=request.initial_capital,
            cost_model=_cost_model_for(found[symbol]),
            status=BacktestStatus.QUEUED,
            engine_version=ENGINE_VERSION,
        )
        for symbol in request.symbols
    ]
    session.add_all(runs)
    session.commit()
    for run in runs:
        session.refresh(run)

    # Enqueued only after the commit, exactly as the single-run endpoint does it. A worker is
    # fast enough to pick up a job before an uncommitted row is visible, and would then fail
    # looking up a backtest that does exist.
    for run in runs:
        await queue.enqueue_job(RUN_BACKTEST, str(run.id))

    return CreatedBasket(
        id=basket.id,
        runs=[
            BasketRunOut(
                backtest_id=run.id,
                symbol=symbol,
                status=run.status,
                cost_model=run.cost_model,
                default_spread_points=found[symbol].default_spread_points,
            )
            for symbol, run in zip(request.symbols, runs, strict=True)
        ],
    )


def _aggregate(runs: list[tuple[Backtest, str]], initial_capital: Decimal) -> BasketAggregate:
    """Dispersion across the finished runs. Never a combined account — see `BasketAggregate`.

    A run counts as finished only when it has metrics. `status == done` is not enough on its
    own and the difference is not pedantry: a run can be marked done in the instant before its
    metrics row is written, and reading `net_profit` off that gap would be an exception in a
    reporting endpoint.

    The **median**, not the mean, and the extremes by name. A basket exists to expose the
    market where the strategy falls apart, and an average is precisely the statistic that
    hides one catastrophic symbol behind three good ones.
    """
    finished = [(run, symbol) for run, symbol in runs if run.metrics is not None]
    failed = sum(1 for run, _ in runs if run.status == BacktestStatus.FAILED)

    # Fractions of the capital every run in a basket starts with — which the basket fixes, so
    # unlike the general run comparator these are comparable by construction rather than by
    # the reader remembering to check.
    returns = sorted(
        (run.metrics.net_profit / initial_capital, symbol)  # type: ignore[union-attr]
        for run, symbol in finished
    )

    if not returns:
        return BasketAggregate(
            runs_total=len(runs),
            runs_finished=0,
            runs_failed=failed,
            runs_profitable=0,
            best_symbol=None,
            best_return=None,
            worst_symbol=None,
            worst_return=None,
            median_return=None,
        )

    middle, odd = divmod(len(returns), 2)
    median = returns[middle][0] if odd else (returns[middle - 1][0] + returns[middle][0]) / 2

    return BasketAggregate(
        runs_total=len(runs),
        runs_finished=len(returns),
        runs_failed=failed,
        runs_profitable=sum(1 for value, _ in returns if value > 0),
        worst_return=returns[0][0],
        worst_symbol=returns[0][1],
        best_return=returns[-1][0],
        best_symbol=returns[-1][1],
        median_return=median,
    )


@router.get("/baskets/{basket_id}", response_model=BasketOut, responses=_NOT_FOUND)
def get_basket(basket_id: uuid.UUID, session: SessionDep) -> BasketOut:
    """The basket, every run in it, and how far apart those runs landed.

    The equity curve is deferred for the same reason `/backtests` defers it: it is a large
    JSONB column that no field of this response can reach, and loading it would ship megabytes
    to the application to build rows nothing reads.
    """
    basket = session.get(Basket, basket_id)
    if basket is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="basket not found")

    strategy = session.get(Strategy, basket.strategy_id)
    if strategy is None:  # pragma: no cover — the FK is RESTRICT, so this cannot happen
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="strategy not found")

    rows = session.execute(
        select(Backtest, Instrument.symbol)
        .join(Instrument, Instrument.id == Backtest.instrument_id)
        .where(Backtest.basket_id == basket.id)
        .options(selectinload(Backtest.metrics).defer(BacktestMetrics.equity_curve))
        .order_by(Instrument.symbol)
    ).all()
    runs = [(run, symbol) for run, symbol in rows]

    return BasketOut(
        id=basket.id,
        strategy_id=basket.strategy_id,
        strategy_name=strategy.name,
        strategy_version=strategy.version,
        timeframe=basket.timeframe,
        date_from=basket.date_from,
        date_to=basket.date_to,
        initial_capital=basket.initial_capital,
        created_at=basket.created_at,
        aggregate=_aggregate(runs, basket.initial_capital),
        runs=[list_item(run, symbol, strategy.name, strategy.version) for run, symbol in runs],
    )

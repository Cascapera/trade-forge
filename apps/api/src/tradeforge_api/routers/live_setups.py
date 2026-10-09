"""`/live-setups` — the setups the live signals follow, their markets and their history (09/10).

His ask: a setup chosen from a run ("Watch this setup live") or registered by hand, its markets
added or dropped at any time, and its signals kept as history with the metrics a run has — in R.
Turning a setup or a market off keeps it and stops its signals; removing forgets it, but its
signals stay in the history.
"""

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, status

from tradeforge_api.deps import SessionDep
from tradeforge_api.routers.strategies import assert_runnable_at, validate_document
from tradeforge_api.schemas import (
    LiveMarketAdd,
    LiveMarketOut,
    LiveMarketPatch,
    LiveMetricsOut,
    LiveSetupCreate,
    LiveSetupFromRun,
    LiveSetupNew,
    LiveSetupOut,
    LiveSetupPatch,
    LiveSetupVersion,
    SignalOut,
)
from tradeforge_db.live_setups import (
    AlreadyFollowedError,
    SetupView,
    add_market,
    create_setup,
    create_with_strategy,
    delete_setup,
    follow_backtest,
    list_setups,
    new_version,
    remove_market,
    set_market,
    setup_metrics,
    signals_of,
    update_setup,
)
from tradeforge_db.models import SignalRecord

router = APIRouter(tags=["live-setups"])

_Responses = dict[int | str, dict[str, Any]]
_NOT_FOUND: _Responses = {
    status.HTTP_404_NOT_FOUND: {"description": "no such setup, run or market"}
}
_CONFLICT: _Responses = {
    status.HTTP_409_CONFLICT: {"description": "already followed on that market by another setup"}
}
_BOTH: _Responses = {**_NOT_FOUND, **_CONFLICT}


def _out(session: SessionDep, view: SetupView) -> LiveSetupOut:
    setup = view.setup
    metrics = setup_metrics(session, setup.id)
    return LiveSetupOut(
        id=setup.id,
        name=setup.name,
        strategy_id=setup.strategy_id,
        strategy_name=view.strategy_name,
        timeframe=setup.timeframe,
        no_target_r=setup.no_target_r,
        active=setup.active,
        note=setup.note,
        source_backtest_id=setup.source_backtest_id,
        created_at=setup.created_at,
        markets=[
            LiveMarketOut(
                instrument_id=market.instrument_id,
                symbol=market.symbol,
                broker=market.broker,
                active=market.active,
            )
            for market in view.markets
        ],
        metrics=LiveMetricsOut(
            signals=metrics.signals,
            closed=metrics.closed,
            open=metrics.open,
            cancelled=metrics.cancelled,
            wins=metrics.wins,
            win_rate=metrics.win_rate,
            net_r=metrics.net_r,
            average_r=metrics.average_r,
            profit_factor=metrics.profit_factor,
            max_drawdown_r=metrics.max_drawdown_r,
            r_by_month=metrics.r_by_month,
        ),
    )


def _one(session: SessionDep, setup_id: uuid.UUID) -> LiveSetupOut:
    view = next((one for one in list_setups(session) if one.setup.id == setup_id), None)
    if view is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no live setup with id {setup_id}")
    return _out(session, view)


def _answer(session: SessionDep, setup_id: uuid.UUID) -> LiveSetupOut:
    session.commit()
    session.expire_all()  # what was stored, as every later read will show it
    return _one(session, setup_id)


@router.get("/live-setups", response_model=list[LiveSetupOut])
def listed(session: SessionDep) -> list[LiveSetupOut]:
    """Every setup, running first, newest first, with its markets and metrics."""
    return [_out(session, view) for view in list_setups(session)]


@router.post(
    "/live-setups/from-run",
    response_model=LiveSetupOut,
    status_code=status.HTTP_201_CREATED,
    responses=_BOTH,
)
def from_run(session: SessionDep, request: LiveSetupFromRun) -> LiveSetupOut:
    """Follow what a finished run was — "Watch this setup live"."""
    try:
        setup = follow_backtest(session, request.backtest_id, no_target_r=request.no_target_r)
    except AlreadyFollowedError as clash:
        raise HTTPException(status.HTTP_409_CONFLICT, str(clash)) from None
    except LookupError as missing:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(missing)) from None
    return _answer(session, setup.id)


@router.post(
    "/live-setups",
    response_model=LiveSetupOut,
    status_code=status.HTTP_201_CREATED,
    responses=_BOTH,
)
def create(session: SessionDep, request: LiveSetupCreate) -> LiveSetupOut:
    """A setup registered by hand, following these markets."""
    try:
        setup = create_setup(
            session,
            strategy_id=request.strategy_id,
            timeframe=request.timeframe,
            cost_model=request.cost_model,
            instrument_ids=request.instrument_ids,
            name=request.name,
            no_target_r=request.no_target_r,
        )
    except AlreadyFollowedError as clash:
        raise HTTPException(status.HTTP_409_CONFLICT, str(clash)) from None
    except LookupError as missing:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(missing)) from None
    return _answer(session, setup.id)


@router.post(
    "/live-setups/new",
    response_model=LiveSetupOut,
    status_code=status.HTTP_201_CREATED,
    responses={
        **_BOTH,
        status.HTTP_422_UNPROCESSABLE_CONTENT: {"description": "the strategy cannot run"},
    },
)
def registered(session: SessionDep, request: LiveSetupNew) -> LiveSetupOut:
    """Register a setup from scratch — its strategy and its markets — and start following it.

    Validated exactly as a strategy saved from the builder, at the chart chosen here."""
    validate_document(request.definition)
    assert_runnable_at(request.definition, request.timeframe)
    try:
        setup = create_with_strategy(
            session,
            definition=request.definition,
            timeframe=request.timeframe,
            instrument_ids=request.instrument_ids,
            cost_model=request.cost_model,
            no_target_r=request.no_target_r,
        )
    except AlreadyFollowedError as clash:
        raise HTTPException(status.HTTP_409_CONFLICT, str(clash)) from None
    except LookupError as missing:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(missing)) from None
    return _answer(session, setup.id)


@router.patch("/live-setups/{setup_id}", response_model=LiveSetupOut, responses=_BOTH)
def change(session: SessionDep, setup_id: uuid.UUID, request: LiveSetupPatch) -> LiveSetupOut:
    """Rename it, switch it on or off, change its no-target R or note."""
    try:
        update_setup(
            session,
            setup_id,
            name=request.name,
            active=request.active,
            no_target_r=request.no_target_r,
            note=request.note,
        )
    except AlreadyFollowedError as clash:
        raise HTTPException(status.HTTP_409_CONFLICT, str(clash)) from None
    except LookupError as missing:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(missing)) from None
    return _answer(session, setup_id)


@router.post(
    "/live-setups/{setup_id}/version",
    response_model=LiveSetupOut,
    status_code=status.HTTP_201_CREATED,
    responses={
        **_NOT_FOUND,
        status.HTTP_422_UNPROCESSABLE_CONTENT: {"description": "the edited strategy cannot run"},
    },
)
def edited(session: SessionDep, setup_id: uuid.UUID, request: LiveSetupVersion) -> LiveSetupOut:
    """Edit the setup's parameters: a new version of its strategy, which the setup now runs.

    Validated exactly as a strategy saved from the builder, and at the setup's own timeframe."""
    current = next((one for one in list_setups(session) if one.setup.id == setup_id), None)
    if current is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no live setup with id {setup_id}")
    validate_document(request.definition)
    assert_runnable_at(request.definition, current.setup.timeframe)
    new_version(session, setup_id, request.definition)
    return _answer(session, setup_id)


@router.delete(
    "/live-setups/{setup_id}", status_code=status.HTTP_204_NO_CONTENT, responses=_NOT_FOUND
)
def remove(session: SessionDep, setup_id: uuid.UUID) -> None:
    """Stop following it for good; its signals stay in the history."""
    try:
        delete_setup(session, setup_id)
    except LookupError as missing:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(missing)) from None
    session.commit()


@router.post("/live-setups/{setup_id}/markets", response_model=LiveSetupOut, responses=_BOTH)
def market_added(session: SessionDep, setup_id: uuid.UUID, request: LiveMarketAdd) -> LiveSetupOut:
    """Follow one more market — or switch one back on."""
    try:
        add_market(session, setup_id, request.instrument_id)
    except AlreadyFollowedError as clash:
        raise HTTPException(status.HTTP_409_CONFLICT, str(clash)) from None
    except LookupError as missing:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(missing)) from None
    return _answer(session, setup_id)


@router.patch(
    "/live-setups/{setup_id}/markets/{instrument_id}", response_model=LiveSetupOut, responses=_BOTH
)
def market_changed(
    session: SessionDep, setup_id: uuid.UUID, instrument_id: uuid.UUID, request: LiveMarketPatch
) -> LiveSetupOut:
    """Switch one market on or off."""
    try:
        set_market(session, setup_id, instrument_id, active=request.active)
    except AlreadyFollowedError as clash:
        raise HTTPException(status.HTTP_409_CONFLICT, str(clash)) from None
    except LookupError as missing:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(missing)) from None
    return _answer(session, setup_id)


@router.delete(
    "/live-setups/{setup_id}/markets/{instrument_id}",
    response_model=LiveSetupOut,
    responses=_NOT_FOUND,
)
def market_removed(
    session: SessionDep, setup_id: uuid.UUID, instrument_id: uuid.UUID
) -> LiveSetupOut:
    """Drop a market; its signals stay in the history."""
    try:
        remove_market(session, setup_id, instrument_id)
    except LookupError as missing:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(missing)) from None
    return _answer(session, setup_id)


@router.get("/live-setups/{setup_id}/signals", response_model=list[SignalOut], responses=_NOT_FOUND)
def signals(
    session: SessionDep,
    setup_id: uuid.UUID,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0, le=1_000_000)] = 0,
) -> list[SignalRecord]:
    """The setup's signals, newest first."""
    _one(session, setup_id)
    return signals_of(session, setup_id, limit=limit, offset=offset)

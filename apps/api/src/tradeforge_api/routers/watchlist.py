"""`/watchlist` — the setups the live signals follow (signals PR 4).

An item is made from a finished run and copies it, so the run can be cleaned away later. Turning
an item off keeps it (and its history) and stops its signals; removing it forgets it.
"""

import uuid
from typing import Any

from fastapi import APIRouter, HTTPException, status

from tradeforge_api.deps import SessionDep
from tradeforge_api.schemas import WatchItemCreate, WatchItemOut, WatchItemPatch
from tradeforge_db.watchlist import (
    AlreadyWatchedError,
    WatchedRow,
    list_watch_items,
    remove_watch_item,
    set_watch_item,
    watch_backtest,
)

router = APIRouter(tags=["watchlist"])

_Responses = dict[int | str, dict[str, Any]]
_NOT_FOUND: _Responses = {status.HTTP_404_NOT_FOUND: {"description": "no such item or run"}}
_CONFLICT: _Responses = {
    status.HTTP_409_CONFLICT: {"description": "this setup is already followed on this market"}
}


def _out(row: WatchedRow) -> WatchItemOut:
    item = row.item
    return WatchItemOut(
        id=item.id,
        symbol=row.symbol,
        broker=row.broker,
        strategy_id=item.strategy_id,
        strategy_name=row.strategy_name,
        timeframe=item.timeframe,
        no_target_r=item.no_target_r,
        active=item.active,
        note=item.note,
        source_backtest_id=item.source_backtest_id,
        created_at=item.created_at,
    )


def _row(session: SessionDep, item_id: uuid.UUID) -> WatchItemOut:
    return next(_out(row) for row in list_watch_items(session) if row.item.id == item_id)


@router.get("/watchlist", response_model=list[WatchItemOut])
def listed(session: SessionDep) -> list[WatchItemOut]:
    """Every item, active first, then newest first."""
    return [_out(row) for row in list_watch_items(session)]


@router.post(
    "/watchlist",
    response_model=WatchItemOut,
    status_code=status.HTTP_201_CREATED,
    responses={**_NOT_FOUND, **_CONFLICT},
)
def create(session: SessionDep, request: WatchItemCreate) -> WatchItemOut:
    """Follow what this run was: its strategy, market, timeframe and costs."""
    try:
        item = watch_backtest(
            session, request.backtest_id, no_target_r=request.no_target_r, note=request.note
        )
    except AlreadyWatchedError as clash:
        raise HTTPException(status.HTTP_409_CONFLICT, str(clash)) from None
    except LookupError as missing:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(missing)) from None
    session.commit()
    session.refresh(item)  # the stored values, as every later read will show them
    return _row(session, item.id)


@router.patch(
    "/watchlist/{item_id}", response_model=WatchItemOut, responses={**_NOT_FOUND, **_CONFLICT}
)
def change(session: SessionDep, item_id: uuid.UUID, request: WatchItemPatch) -> WatchItemOut:
    """Turn it on or off, or change its R or note."""
    try:
        item = set_watch_item(
            session,
            item_id,
            active=request.active,
            no_target_r=request.no_target_r,
            note=request.note,
        )
    except AlreadyWatchedError as clash:
        raise HTTPException(status.HTTP_409_CONFLICT, str(clash)) from None
    except LookupError as missing:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(missing)) from None
    session.commit()
    session.refresh(item)
    return _row(session, item_id)


@router.delete("/watchlist/{item_id}", status_code=status.HTTP_204_NO_CONTENT, responses=_NOT_FOUND)
def remove(session: SessionDep, item_id: uuid.UUID) -> None:
    """Stop following it for good."""
    try:
        remove_watch_item(session, item_id)
    except LookupError as missing:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(missing)) from None
    session.commit()

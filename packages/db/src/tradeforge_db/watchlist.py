"""The watchlist: what the live signals follow (signals PR 4).

Every item is made from a finished run and copies what the run was — strategy, market, timeframe,
costs — so the run can be cleaned away and the item still says exactly what to follow.
"""

import uuid
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from tradeforge_db.models import (
    Backtest,
    BacktestStatus,
    Broker,
    Instrument,
    Strategy,
    WatchItem,
)

__all__ = [
    "AlreadyWatchedError",
    "WatchedRow",
    "list_watch_items",
    "remove_watch_item",
    "set_watch_item",
    "watch_backtest",
]


class AlreadyWatchedError(ValueError):
    """The same setup on the same market is already followed — it would post every signal twice."""


@dataclass(frozen=True, slots=True)
class WatchedRow:
    """An item with the names a screen shows beside it."""

    item: WatchItem
    symbol: str
    broker: str | None
    strategy_name: str


def watch_backtest(
    session: Session,
    backtest_id: uuid.UUID,
    *,
    no_target_r: Decimal = Decimal(5),
    note: str | None = None,
) -> WatchItem:
    """Follow what this run was. `LookupError` for no such run or one not finished;
    `AlreadyWatchedError` when an active item already follows the same thing."""
    run = session.get(Backtest, backtest_id)
    if run is None:
        raise LookupError(f"no run with id {backtest_id}")
    if run.status is not BacktestStatus.DONE:
        raise LookupError(f"run {backtest_id} is {run.status.value}, not done: nothing to follow")
    item = WatchItem(
        strategy_id=run.strategy_id,
        instrument_id=run.instrument_id,
        timeframe=run.timeframe,
        cost_model=dict(run.cost_model),
        source_backtest_id=run.id,
        no_target_r=no_target_r,
        note=note,
    )
    session.add(item)
    try:
        with session.begin_nested():
            session.flush()
    except IntegrityError as clash:
        if "uq_watch_items_active" in str(clash.orig):
            raise AlreadyWatchedError(
                "this setup is already followed on this market and timeframe"
            ) from None
        raise
    return item


def list_watch_items(session: Session) -> list[WatchedRow]:
    """Every item, active first, then newest first."""
    statement = (
        select(WatchItem, Instrument.symbol, Broker.slug, Strategy.name)
        .join(Instrument, Instrument.id == WatchItem.instrument_id)
        .outerjoin(Broker, Broker.id == Instrument.broker_id)
        .join(Strategy, Strategy.id == WatchItem.strategy_id)
        .order_by(WatchItem.active.desc(), WatchItem.created_at.desc(), WatchItem.id)
    )
    return [
        WatchedRow(item=item, symbol=symbol, broker=broker, strategy_name=name)
        for item, symbol, broker, name in session.execute(statement)
    ]


def set_watch_item(
    session: Session,
    item_id: uuid.UUID,
    *,
    active: bool | None = None,
    no_target_r: Decimal | None = None,
    note: str | None = None,
) -> WatchItem:
    """Change an item. `LookupError` if there is none; `AlreadyWatchedError` turning one on
    while another active item follows the same thing."""
    item = session.get(WatchItem, item_id)
    if item is None:
        raise LookupError(f"no watch item with id {item_id}")
    if active is not None:
        item.active = active
    if no_target_r is not None:
        item.no_target_r = no_target_r
    if note is not None:
        item.note = note or None
    try:
        with session.begin_nested():
            session.flush()
    except IntegrityError as clash:
        if "uq_watch_items_active" in str(clash.orig):
            raise AlreadyWatchedError(
                "another active item already follows this setup on this market and timeframe"
            ) from None
        raise
    return item


def remove_watch_item(session: Session, item_id: uuid.UUID) -> None:
    """Stop following it for good. `LookupError` if there is none."""
    item = session.get(WatchItem, item_id)
    if item is None:
        raise LookupError(f"no watch item with id {item_id}")
    session.delete(item)
    session.flush()

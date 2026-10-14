"""The share splits a stored history is adjusted for: writing one down and reading them back.

The arithmetic — which bars, divided by what, and how a file remembers it was — lives in the
collector, the only process that writes candles. This module only knows the rows.
"""

import datetime as dt
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from tradeforge_db.models import Instrument, StockSplit
from tradeforge_engine.domain import AssetClass

__all__ = ["RecordedSplit", "StockMarket", "record_split", "splits_of", "stock_markets"]


@dataclass(frozen=True, slots=True)
class RecordedSplit:
    """One split as the collector needs it: the share, the day, the ratio, and the clock.

    `server_offset` travels with it because `effective_on` is a day on the *broker's* clock, and
    the bars are stored in UTC: the instant the new price starts is that day's midnight there.
    """

    symbol: str
    effective_on: dt.date
    shares_after: int
    shares_before: int
    server_offset: dt.timedelta


@dataclass(frozen=True, slots=True)
class StockMarket:
    """A share this system collects, and the clock its bars were shifted by."""

    symbol: str
    server_offset: dt.timedelta


def splits_of(session: Session, symbol: str) -> tuple[RecordedSplit, ...]:
    """Every split recorded for `symbol`, oldest first — empty for a symbol with none."""
    rows = session.execute(
        select(StockSplit, Instrument.symbol, Instrument.server_offset)
        .join(Instrument, Instrument.id == StockSplit.instrument_id)
        .where(Instrument.symbol == symbol)
        .order_by(StockSplit.effective_on)
    ).all()
    return tuple(
        RecordedSplit(
            symbol=name,
            effective_on=split.effective_on,
            shares_after=split.shares_after,
            shares_before=split.shares_before,
            server_offset=offset,
        )
        for split, name, offset in rows
    )


def record_split(
    session: Session, symbol: str, effective_on: dt.date, *, shares_after: int, shares_before: int
) -> RecordedSplit:
    """Write one split down. Flushed, not committed: the caller rewrites the files first.

    ⚠️ **Refused twice for the same day**, and said so in words: the same split recorded again
    would be applied again, and a history divided by twenty twice is worse than one not divided.
    """
    instrument = session.execute(
        select(Instrument).where(Instrument.symbol == symbol)
    ).scalar_one_or_none()
    if instrument is None:
        raise LookupError(f"no instrument called {symbol}")

    session.add(
        StockSplit(
            instrument_id=instrument.id,
            effective_on=effective_on,
            shares_after=shares_after,
            shares_before=shares_before,
        )
    )
    try:
        session.flush()
    except IntegrityError as error:
        session.rollback()
        raise ValueError(
            f"{symbol} already has a split on {effective_on:%Y-%m-%d}, or {shares_after} for "
            f"{shares_before} is not a split"
        ) from error

    return RecordedSplit(
        symbol=symbol,
        effective_on=effective_on,
        shares_after=shares_after,
        shares_before=shares_before,
        server_offset=instrument.server_offset,
    )


def stock_markets(session: Session) -> list[StockMarket]:
    """Every share catalogued — what a search for unrecorded splits looks through."""
    rows = session.execute(
        select(Instrument.symbol, Instrument.server_offset)
        .where(Instrument.asset_class == AssetClass.STOCK)
        .order_by(Instrument.symbol)
    ).all()
    return [StockMarket(symbol=symbol, server_offset=offset) for symbol, offset in rows]

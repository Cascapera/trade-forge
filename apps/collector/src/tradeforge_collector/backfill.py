"""The backfill: source → Parquet → catalogue → gap report.

Depends on `MarketDataSource`, never on MetaTrader. That one line is what lets this
function — the actual product — be tested end to end on Linux, on every push.

Idempotent from end to end, because a backfill *is* re-run: nightly, after a gap turns
up, after a broker changes a contract size. The Parquet write replaces the year
partitions it produces; the instrument and dataset rows are upserts. Run it twice and
the world looks the same as after running it once.
"""

import datetime as dt
import logging
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from tradeforge_collector.gaps import Gap, find_gaps
from tradeforge_collector.source import Candle, MarketDataSource
from tradeforge_collector.storage import write_candles
from tradeforge_collector.timeframes import step
from tradeforge_db.instruments import InstrumentSpec, upsert_dataset, upsert_instruments
from tradeforge_db.models import Instrument

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class BackfillReport:
    """What one backfill did — the thing a human reads and a test asserts on."""

    instrument: InstrumentSpec
    timeframe: str
    candles: int
    date_from: dt.datetime
    date_to: dt.datetime
    parquet_path: str
    gaps: list[Gap]


def backfill(  # noqa: PLR0913 — all keyword-only, and each one names a real axis of the job
    source: MarketDataSource,
    *,
    root: Path,
    symbol: str,
    timeframe: str,
    start: dt.datetime,
    end: dt.datetime,
    session: Session | None = None,
) -> BackfillReport:
    """Download a symbol's history, store it, catalogue it, and report the holes.

    `session` is optional so the data path can be exercised without a database — the
    Parquet is the product, the catalogue is bookkeeping about it.
    """
    _require_utc(start, "start")
    _require_utc(end, "end")
    if end < start:
        raise ValueError(f"end ({end}) is before start ({start})")

    spec = source.instrument(symbol)
    candles = source.candles(symbol, timeframe, start, end)
    if not candles:
        raise LookupError(f"no candles for {symbol} {timeframe} between {start} and {end}")

    _assert_ascending(candles, symbol, timeframe)

    parquet_path = write_candles(root, symbol, timeframe, candles)
    gaps = find_gaps([candle.time for candle in candles], step(timeframe))

    report = BackfillReport(
        instrument=spec,
        timeframe=timeframe,
        candles=len(candles),
        date_from=candles[0].time,
        date_to=candles[-1].time,
        parquet_path=parquet_path,
        gaps=gaps,
    )

    if session is not None:
        _catalogue(session, report)

    logger.info("backfilled %s %s: %d candles, %d gaps", symbol, timeframe, len(candles), len(gaps))
    return report


def _catalogue(session: Session, report: BackfillReport) -> None:
    """Record the instrument and the coverage. Both upserts — see `tradeforge_db`."""
    upsert_instruments(session, (report.instrument,))

    instrument_id = session.execute(
        select(Instrument.id).where(Instrument.symbol == report.instrument.symbol)
    ).scalar_one()

    upsert_dataset(
        session,
        instrument_id=instrument_id,
        timeframe=report.timeframe,
        date_from=report.date_from,
        date_to=report.date_to,
        candle_count=report.candles,
        parquet_path=report.parquet_path,
    )


def _assert_ascending(candles: list[Candle], symbol: str, timeframe: str) -> None:
    """A source that returns bars out of order hands the engine a lookahead.

    Checked rather than fixed. Sorting silently here would mean a broken source keeps
    working, and the next one that breaks differently goes unnoticed too.
    """
    for previous, current in pairwise(candles):
        if current.time <= previous.time:
            raise ValueError(
                f"{symbol} {timeframe}: candles are not ascending "
                f"({previous.time} then {current.time})"
            )


def _require_utc(moment: dt.datetime, name: str) -> None:
    if moment.tzinfo is None:
        raise ValueError(f"{name} must be timezone-aware; a naive datetime means nothing here")

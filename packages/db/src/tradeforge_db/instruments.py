"""Writing instruments and datasets — the collector's landing zone.

The collector (PR-102) discovers the truth about a symbol from MT5 `symbol_info`
and the extent of what it downloaded. Both writes are **upserts**, because a
backfill is re-run: nightly, after a gap is found, after a symbol's contract
specification changes. A second run must converge on the same rows, not fail on a
conflict and not append a contradictory second account of the same range.

`InstrumentSpec` is the shape both the collector and the seeds speak. Keeping one
dataclass rather than two means the example instruments and the real ones from MT5
go through exactly the same code path — so the path the seeds exercise every day is
the path production uses.
"""

import datetime as dt
from dataclasses import asdict

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from tradeforge_db.models import Dataset, Instrument
from tradeforge_engine.domain import InstrumentSpec

# Everything except the natural key. Re-running a backfill after a broker changes a
# contract size must update the row, not silently keep the stale number that every
# subsequent P&L would be computed against.
_INSTRUMENT_UPDATABLE = (
    "name",
    "asset_class",
    "exchange",
    "currency_base",
    "currency_quote",
    "tick_size",
    "tick_value",
    "contract_size",
    "digits",
)


def upsert_instruments(session: Session, specs: tuple[InstrumentSpec, ...]) -> int:
    """Insert the instruments, updating any symbol that already exists.

    Idempotent: running it twice leaves the same rows. Returns how many were written.
    """
    if not specs:
        return 0

    rows = [asdict(spec) for spec in specs]

    statement = insert(Instrument).values(rows)
    statement = statement.on_conflict_do_update(
        index_elements=[Instrument.symbol],
        set_={column: statement.excluded[column] for column in _INSTRUMENT_UPDATABLE},
    )
    session.execute(statement)
    return len(rows)


def upsert_dataset(  # noqa: PLR0913 — keyword-only; these are simply the columns of the row
    session: Session,
    *,
    instrument_id: object,
    timeframe: str,
    date_from: dt.datetime,
    date_to: dt.datetime,
    candle_count: int,
    parquet_path: str,
) -> None:
    """Record what exists in Parquet for one (instrument, timeframe).

    One row per pair — the unique constraint from PR-101 is what makes that true, and
    what turns a re-backfill into an update of the coverage this catalogue already
    claims rather than a second, contradictory row about the same files.
    """
    statement = insert(Dataset).values(
        instrument_id=instrument_id,
        timeframe=timeframe,
        date_from=date_from,
        date_to=date_to,
        candle_count=candle_count,
        parquet_path=parquet_path,
    )
    statement = statement.on_conflict_do_update(
        index_elements=[Dataset.instrument_id, Dataset.timeframe],
        set_={
            "date_from": statement.excluded.date_from,
            "date_to": statement.excluded.date_to,
            "candle_count": statement.excluded.candle_count,
            "parquet_path": statement.excluded.parquet_path,
            "collected_at": dt.datetime.now(tz=dt.UTC),
        },
    )
    session.execute(statement)

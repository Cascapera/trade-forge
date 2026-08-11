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
from dataclasses import asdict, dataclass
from decimal import Decimal

from sqlalchemy import func
from sqlalchemy.dialects.postgresql import Insert, insert
from sqlalchemy.orm import Session

from tradeforge_db.models import Dataset, Instrument
from tradeforge_engine.domain import InstrumentSpec


@dataclass(frozen=True, slots=True)
class CatalogueEntry:
    """An `InstrumentSpec` plus what the catalogue knows and the engine deliberately does not.

    The split is the point. `InstrumentSpec` is the engine's type — "a symbol and the numbers
    that turn a price move into money" — and the engine's pricing genuinely does not need a
    spread: under ADR-07 costs are plugged into a run as a `CostModel`, never read off the
    instrument. Adding `default_spread_points` to that dataclass would have been one line
    fewer here and an open invitation for a later change to have the engine read it directly,
    which is precisely the coupling ADR-07 exists to prevent.

    So the field lives on the catalogue's own type instead. The database stores it because it
    is broker-quoted data like `tick_value`; the screen reads it to pre-fill a cost model; the
    engine never sees it.

    `default_spread_points` is in **ticks**, already converted from whatever unit the source
    quoted it in, and `None` means nobody has measured this symbol — never zero, which would
    claim the instrument is free to trade.
    """

    spec: InstrumentSpec
    default_spread_points: Decimal | None = None


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
    "default_spread_points",
)


def upsert_instruments(
    session: Session,
    entries: tuple[CatalogueEntry | InstrumentSpec, ...],
    *,
    overwrite: bool = True,
) -> int:
    """Insert the instruments. Returns how many rows the database actually wrote.

    Idempotent either way: running it twice leaves the same rows.

    A bare `InstrumentSpec` is accepted as well as a `CatalogueEntry`, and means "this source
    has nothing to say about the spread" — which writes NULL rather than zero. The seeds are
    the reason: they are hand-written example instruments with no broker behind them, and
    claiming a spread of zero for them would be inventing a measurement.

    `overwrite` is what separates the two callers, and it exists because collapsing them cost
    real data. The collector states the broker's truth and must win over whatever is there.
    The seeds only guarantee the example symbols exist so a fresh machine has something to
    run — but they went through the identical upsert, so `docker compose up` (which runs the
    seed step every time) rewrote all ten updatable columns with placeholders.

    That was invisible until PR-226 added `default_spread_points`, because the placeholder
    specs were chosen to match what MT5 reports: rewriting `tick_size` with the same
    `tick_size` changes nothing observable. The spread is the first column where the seeds
    have nothing and the collector has something, so it is the first place the overwrite
    could be seen — measured on 11-08-2026, a catalogued EURUSD 8 / GBPUSD 9 was back to
    NULL within minutes, wiped by a stack restart.

    ⚠️ The cost of `overwrite=False`: a corrected placeholder never reaches a database that
    already has the row. Accepted, and the alternative is worse — a seed silently outranking
    a measurement is how the data was lost in the first place, and the broker is the
    authority on every one of these columns anyway.
    """
    if not entries:
        return 0

    rows = [
        {
            **asdict(entry.spec if isinstance(entry, CatalogueEntry) else entry),
            "default_spread_points": (
                entry.default_spread_points if isinstance(entry, CatalogueEntry) else None
            ),
        }
        for entry in entries
    ]

    statement = insert(Instrument).values(rows)
    if not overwrite:
        statement = statement.on_conflict_do_nothing(index_elements=[Instrument.symbol])
        return _written(session, statement)

    statement = statement.on_conflict_do_update(
        index_elements=[Instrument.symbol],
        set_={
            **{column: statement.excluded[column] for column in _INSTRUMENT_UPDATABLE},
            # Stamped here rather than left to the column's `onupdate`, which does not fire:
            # that hook belongs to the ORM's UPDATE, and this is a Core ON CONFLICT whose SET
            # clause is used exactly as written. Absent from it, the stamp keeps the value the
            # row was first inserted with — so the catalogue would carry a fresh spread under
            # a stale date, which reads as a measurement and is not one.
            #
            # `func.now()` and not the process clock: the writer here is the collector on a
            # Windows host and the row lives in a container, two clocks that need not agree.
            # `models._created_at` states the rule — the database's clock is the shared one.
            # (The sibling `upsert_dataset` stamps `collected_at` from Python; same defect,
            # smaller blast radius, noted in `specs/backlog.md`.)
            #
            # Postgres evaluates `now()` at transaction start, so every symbol in one
            # `catalogue` call shares a stamp. That is the honest reading: they were read
            # from the terminal in one pass.
            "updated_at": func.now(),
        },
    )
    return _written(session, statement)


def _written(session: Session, statement: Insert) -> int:
    """Run the upsert and count the rows the database actually wrote.

    `len(rows)` would be the count of rows *offered*, which under `DO UPDATE` happens to be
    the same number and under `DO NOTHING` is not: re-seeding a populated catalogue writes
    nothing and would still have reported four. `tradeforge-db seed` prints this straight to
    the migrate log, so the wrong number is not an internal detail — it is the line an
    operator reads to find out what a deploy did.
    """
    return len(session.execute(statement.returning(Instrument.symbol)).all())


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

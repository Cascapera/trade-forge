"""Collecting a range: how it is cut up, who writes the result down, and what it is worth.

The download itself is `backfill()`, which already exists and is already idempotent. What is
added here is the three things a *range* needs that a single call does not: a cut small enough
to hold in memory, tolerance for a year the broker has nothing for, and a catalogue entry that
describes the files rather than this particular run.

## Why a range is cut at all

`source.candles()` is one `copy_rates_range` call and the whole answer lands in memory as
`Candle` objects, each holding four `Decimal` prices. This project's own default window is
368,500 bars, and the terminal on this machine will now offer ten million on M1. Asking for all
of it at once is one allocation the host cannot take back.

Cutting at the **year** rather than at some number of bars is not arbitrary: the Parquet layout
is already partitioned by year (ADR-05), so a slice maps exactly onto the partitions
`write_candles` replaces. A slice that straddled a boundary would make two runs rewrite each
other's files; a slice that *is* the boundary cannot.

And the progress a person watches falls out of the same cut for free — one slice done is one
year done, which is a sentence somebody can act on.
"""

import datetime as dt
import logging
import uuid
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from tradeforge_collector.backfill import BackfillReport, backfill
from tradeforge_collector.gaps import find_gaps
from tradeforge_collector.source import MarketDataSource
from tradeforge_collector.storage import Coverage, coverage, dataset_path, read_times
from tradeforge_collector.timeframes import step
from tradeforge_db.collections import finish_collection, record_progress, start_collection
from tradeforge_db.instruments import CatalogueEntry, upsert_dataset, upsert_instruments
from tradeforge_db.models import Instrument
from tradeforge_engine.domain import InstrumentSpec

logger = logging.getLogger(__name__)

__all__ = [
    "CollectionJournal",
    "CollectionOutcome",
    "Coverage",
    "DatabaseJournal",
    "run_collection",
    "year_slices",
]


@dataclass(frozen=True, slots=True)
class CollectionOutcome:
    """What a whole collection produced, across however many slices it took."""

    candles: int
    slices_with_data: int
    slices_total: int

    @property
    def found_nothing(self) -> bool:
        """⚠️ Every slice came back empty, which is the only failure among the empty cases.

        A single empty year inside a range is ordinary — a symbol listed in 2018 and asked for
        from 2015 has three of them, and refusing the whole collection over that would make the
        screen's own suggested window unusable on any recently listed instrument. Every slice
        empty is different: it means the broker has nothing here at all, and finishing quietly
        would write a catalogue row claiming a dataset that does not exist.

        Counted in **slices**, not candles, and today the two cannot disagree: `backfill` raises
        `LookupError` on an empty range, so a slice that returned at all returned at least one
        bar. Slices is still the definition rather than the coincidence, because the sentence
        this produces on screen is about years the broker had nothing for.
        """
        return self.slices_with_data == 0


def year_slices(start: dt.datetime, end: dt.datetime) -> list[tuple[dt.datetime, dt.datetime]]:
    """Cut `[start, end]` at calendar year boundaries, in order.

    Both ends are inclusive, matching `copy_rates_range` and `read_candles` — the caller holds
    a window as *the first bar and the last bar*, and a half-open end would drop the bar the
    window finishes on.

    ⚠️ **The slices touch but never overlap.** Each one ends one microsecond before the next
    begins, so no instant belongs to two of them. Overlap would be invisible rather than
    duplicated — `write_candles` replaces whole year partitions, so a bar written twice by two
    slices lands in one partition and the second write erases the first — which means the
    symptom would not be a duplicate row but a *missing* year, appearing only when a range
    happened to straddle a boundary.

    Raises rather than returning nothing when `end` precedes `start`: a backwards range is a
    caller bug, and an empty list would be indistinguishable from a range with no data in it.
    """
    if end < start:
        raise ValueError(f"end ({end}) is before start ({start})")

    slices: list[tuple[dt.datetime, dt.datetime]] = []
    cursor = start
    while cursor <= end:
        year_ends = _last_instant_of(cursor.year, tzinfo=start.tzinfo)
        slices.append((cursor, min(year_ends, end)))
        cursor = year_ends + _ONE_MICROSECOND

    return slices


_ONE_MICROSECOND = dt.timedelta(microseconds=1)


def _last_instant_of(year: int, *, tzinfo: dt.tzinfo | None) -> dt.datetime:
    """The final microsecond of a year.

    Microseconds because that is the resolution the Parquet `time` column stores (ADR-05); a
    coarser boundary would leave instants inside a second that belong to neither slice.
    """
    return dt.datetime(year + 1, 1, 1, tzinfo=tzinfo) - _ONE_MICROSECOND


class CollectionJournal(Protocol):
    """Where a collection writes down what it is doing. Postgres in production, a list in a test.

    ⚠️ **The same move `backfill` makes with `MarketDataSource`, for the same reason.** That
    function depends on a source rather than on MetaTrader, and that one line is what lets the
    actual product be exercised end to end on Linux CI. This depends on a journal rather than on
    a `Session`, and buys the same thing: the cutting, the empty-year tolerance and every
    failure path run in the unit suite.

    Without it the orchestration would be reachable only from a test marked `integration` — and
    this project's gate runs `pytest` without those, so the whole of it would count as uncovered
    while looking, from the outside, thoroughly tested.
    """

    def started(self) -> None:
        """The job has been picked up off the queue."""
        ...

    def year_done(self, years_done: int) -> None:
        """One more slice is on disk — or was found empty, which is also done."""
        ...

    def catalogued(self, spec: InstrumentSpec, spread: Decimal | None, on_disk: Coverage) -> None:
        """The instrument and what the files now hold, for the rest of the system to find."""
        ...

    def failed(self, reason: str) -> None:
        """Nothing usable was collected, and here is the sentence to put on the screen."""
        ...

    def finished(self, *, candles: int, gaps: int) -> None:
        """It is done, and this is what the series looks like."""
        ...


def run_collection(  # noqa: PLR0913 — keyword-only; each names a real axis of the request
    source: MarketDataSource,
    journal: CollectionJournal,
    *,
    root: Path,
    symbol: str,
    timeframe: str,
    date_from: dt.datetime,
    date_to: dt.datetime,
) -> CollectionOutcome:
    """Download a range year by year and record what ended up on disk."""
    slices = year_slices(date_from, date_to)
    journal.started()

    # ⚠️ **One list rather than four accumulators, because the four were one fact under four
    # names.** A running candle count, a slice count, and a remembered instrument all changed in
    # the same branch, so "some slice came back with data" could be asked of any of them — and
    # the guard below asked it twice, which mutation testing found by proving that deleting half
    # of it changed nothing observable. Four names for one fact is four chances for a later edit
    # to move one and leave the rest agreeing with each other about the wrong thing.
    # A report is small (counts, a path, two instants), so holding one per year costs nothing.
    reports: list[BackfillReport] = []

    for done, (slice_from, slice_to) in enumerate(slices, start=1):
        try:
            report = backfill(
                source,
                root=root,
                symbol=symbol,
                timeframe=timeframe,
                start=slice_from,
                end=slice_to,
                session=None,
            )
        except LookupError:
            # ⚠️ An empty year is ordinary and must not fail the request. A symbol listed in
            # 2018 asked for from 2015 has three of them, and the screen's own suggested window
            # is what asks — refusing here would make the default unusable on anything recently
            # listed. Every year empty is the real failure, and `found_nothing` says so.
            logger.info("no %s %s bars between %s and %s", symbol, timeframe, slice_from, slice_to)
        else:
            reports.append(report)

        journal.year_done(done)

    outcome = CollectionOutcome(
        candles=sum(report.candles for report in reports),
        slices_with_data=len(reports),
        slices_total=len(slices),
    )

    if outcome.found_nothing:
        journal.failed(
            f"the broker returned no {timeframe} bars for {symbol} anywhere between "
            f"{date_from.date()} and {date_to.date()}"
        )
        return outcome

    # The last slice that had data. Any of them would name the same instrument, but the latest
    # is the one whose spread was measured most recently — and a spread is a quote, not a
    # property of the symbol.
    latest = reports[-1]

    on_disk = coverage(root, symbol, timeframe)
    if on_disk is None:
        # Slices reported candles and the directory holds none — something removed the files
        # between the write and here. Reported rather than raised, because the screen is the
        # only place anybody would see it and a traceback in the agent's log is not the screen.
        journal.failed(f"{symbol} {timeframe} was written but nothing is on disk")
        return outcome

    journal.catalogued(latest.instrument, latest.spread_points, on_disk)
    journal.finished(
        candles=on_disk.candle_count,
        gaps=_count_gaps(root, symbol, timeframe, on_disk.date_from, on_disk.date_to),
    )
    return outcome


def _count_gaps(
    root: Path, symbol: str, timeframe: str, date_from: dt.datetime, date_to: dt.datetime
) -> int:
    """How many interruptions the finished series holds, counted over the **whole** span.

    ⚠️ Not summed per slice, which is what reusing each `BackfillReport.gaps` would have given.
    A gap that straddles New Year — a holiday closure, the commonest kind there is — belongs to
    no single year, so a per-slice sum is always slightly too small and never obviously wrong.
    Reading the times back costs one column of a columnar file (ADR-05).

    The bounds it is called with are `coverage()`'s own span — the whole dataset — so the filter
    selects everything and dropping it would change no number today. It stays because it ties
    the gap count to *the span reported next to it*: the day a caller asks about a sub-range, an
    unfiltered count would answer about a different series without saying so.
    """
    times = read_times(root, symbol, timeframe, start=date_from, end=date_to)
    return len(find_gaps(times, step(timeframe)))


class DatabaseJournal:
    """The production journal: one `collections` row, driven through its states.

    ⚠️ **Commits after every entry, on purpose.** The row is what the screen polls, and a
    collection can take minutes; progress held inside one open transaction would become visible
    only once there was none left to report. The cost is that a crash halfway leaves a row
    saying `running` with three of five years done — which is true, and is exactly what the
    files on disk look like.

    Lives here rather than in `tradeforge_db` because it speaks `Coverage` and `InstrumentSpec`,
    and a shared package importing an app fails `test_shared_packages_never_depend_on_apps`.
    """

    def __init__(
        self, session: Session, collection_id: uuid.UUID, *, root: Path, timeframe: str
    ) -> None:
        self._session = session
        self._id = collection_id
        self._root = root
        self._timeframe = timeframe

    def started(self) -> None:
        start_collection(self._session, self._id, at=_now())
        self._session.commit()

    def year_done(self, years_done: int) -> None:
        record_progress(self._session, self._id, years_done=years_done)
        self._session.commit()

    def catalogued(self, spec: InstrumentSpec, spread: Decimal | None, on_disk: Coverage) -> None:
        upsert_instruments(self._session, (CatalogueEntry(spec, spread),))
        instrument_id = self._session.execute(
            select(Instrument.id).where(Instrument.symbol == spec.symbol)
        ).scalar_one()
        upsert_dataset(
            self._session,
            instrument_id=instrument_id,
            timeframe=self._timeframe,
            date_from=on_disk.date_from,
            date_to=on_disk.date_to,
            candle_count=on_disk.candle_count,
            parquet_path=dataset_path(self._root, spec.symbol, self._timeframe),
        )
        self._session.commit()

    def failed(self, reason: str) -> None:
        finish_collection(self._session, self._id, at=_now(), error=reason)
        self._session.commit()

    def finished(self, *, candles: int, gaps: int) -> None:
        finish_collection(self._session, self._id, at=_now(), candles=candles, gaps=gaps)
        self._session.commit()


def _now() -> dt.datetime:
    return dt.datetime.now(tz=dt.UTC)

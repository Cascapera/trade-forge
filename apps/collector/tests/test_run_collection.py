"""One collection request, end to end, against a source and a journal — no terminal, no database.

That is the whole reason `run_collection` takes a `CollectionJournal` instead of a `Session`:
these are the paths a person actually hits — a range crossing a year, a year the broker has
nothing for, a symbol it has nothing for at all — and every one of them would otherwise be
reachable only from a test marked `integration`, which this project's coverage gate does not run.
"""

import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

import pytest

from tradeforge_collector.collect import CollectionOutcome, Coverage, run_collection
from tradeforge_collector.source import MarketDataSource
from tradeforge_collector.synthetic import SyntheticSource
from tradeforge_engine.domain import Candle, InstrumentSpec


def utc(year: int, month: int = 1, day: int = 1) -> dt.datetime:
    return dt.datetime(year, month, day, tzinfo=dt.UTC)


@dataclass
class SpyJournal:
    """Every entry the run made, in order. A list is enough to assert a state machine."""

    entries: list[str] = field(default_factory=list)
    catalogued_coverage: Coverage | None = None
    catalogued_spread: Decimal | None = None
    finished_candles: int | None = None
    finished_gaps: int | None = None
    failure: str | None = None

    def started(self) -> None:
        self.entries.append("started")

    def year_done(self, years_done: int) -> None:
        self.entries.append(f"year {years_done}")

    def catalogued(self, spec: InstrumentSpec, spread: Decimal | None, on_disk: Coverage) -> None:
        self.entries.append(f"catalogued {spec.symbol}")
        self.catalogued_coverage = on_disk
        self.catalogued_spread = spread

    def failed(self, reason: str) -> None:
        self.entries.append("failed")
        self.failure = reason

    def finished(self, *, candles: int, gaps: int) -> None:
        self.entries.append("finished")
        self.finished_candles = candles
        self.finished_gaps = gaps


class EmptyBefore:
    """A source that has nothing before `listed`, exactly like a recently listed instrument.

    Wraps the synthetic source rather than replacing it, so the bars that *do* come back are
    the same ones every other test in this suite sees.
    """

    def __init__(self, listed: dt.datetime, *, inner: MarketDataSource | None = None) -> None:
        self._listed = listed
        self._inner = inner or SyntheticSource()

    def instrument(self, symbol: str) -> InstrumentSpec:
        return self._inner.instrument(symbol)

    def spread_points(self, symbol: str) -> Decimal | None:
        return self._inner.spread_points(symbol)

    def candles(
        self, symbol: str, timeframe: str, start: dt.datetime, end: dt.datetime
    ) -> list[Candle]:
        if end < self._listed:
            return []
        return [
            candle
            for candle in self._inner.candles(symbol, timeframe, start, end)
            if candle.time >= self._listed
        ]


class QuotesASpread:
    """A source that quotes a spread, and a different one every time it is asked.

    ⚠️ **`SyntheticSource` answers `None` here** — there is no broker behind it — which is the
    same value a *dropped* spread produces. A fixture whose value equals what the bug would
    produce cannot tell the two apart, so every scenario in this file was blind to the spread
    never reaching the catalogue at all.

    Two different numbers separate three things at once: that a quote arrives, that it is the
    last one rather than the first, and that it is not invented. The pair is the one measured on
    this project's own terminal — 11 points with the market shut, 1 with it open.
    """

    def __init__(self, quotes: list[Decimal]) -> None:
        self._quotes = quotes
        self._inner = SyntheticSource()
        self.asked = 0

    def instrument(self, symbol: str) -> InstrumentSpec:
        return self._inner.instrument(symbol)

    def spread_points(self, symbol: str) -> Decimal | None:
        quote = self._quotes[min(self.asked, len(self._quotes) - 1)]
        self.asked += 1
        return quote

    def candles(
        self, symbol: str, timeframe: str, start: dt.datetime, end: dt.datetime
    ) -> list[Candle]:
        return self._inner.candles(symbol, timeframe, start, end)


class Barren:
    """A source that has never heard of this series, at any date."""

    def instrument(self, symbol: str) -> InstrumentSpec:
        return SyntheticSource().instrument(symbol)

    def spread_points(self, symbol: str) -> Decimal | None:
        return None

    def candles(
        self, symbol: str, timeframe: str, start: dt.datetime, end: dt.datetime
    ) -> list[Candle]:
        return []


@pytest.fixture
def journal() -> SpyJournal:
    return SpyJournal()


def collect(  # noqa: PLR0913 — a test helper mirroring the signature under test
    source: MarketDataSource,
    journal: SpyJournal,
    root: Path,
    *,
    date_from: dt.datetime,
    date_to: dt.datetime,
    timeframe: str = "H1",
) -> CollectionOutcome:
    return run_collection(
        source,
        journal,
        root=root,
        symbol="EURUSD",
        timeframe=timeframe,
        date_from=date_from,
        date_to=date_to,
    )


def test_a_range_inside_one_year_is_collected_and_catalogued(
    tmp_path: Path, journal: SpyJournal
) -> None:
    outcome = collect(
        SyntheticSource(), journal, tmp_path, date_from=utc(2021, 3, 1), date_to=utc(2021, 3, 10)
    )

    assert journal.entries == ["started", "year 1", "catalogued EURUSD", "finished"]
    assert outcome.slices_total == 1
    assert journal.finished_candles is not None
    assert journal.finished_candles > 0


def test_a_range_across_years_reports_one_step_per_year(
    tmp_path: Path, journal: SpyJournal
) -> None:
    """⚠️ Progress in the unit the work is done in. Three years, three steps — and the screen
    can say "2 of 3 years", which a fraction cannot."""
    collect(
        SyntheticSource(), journal, tmp_path, date_from=utc(2020, 11, 1), date_to=utc(2022, 2, 1)
    )

    assert journal.entries == [
        "started",
        "year 1",
        "year 2",
        "year 3",
        "catalogued EURUSD",
        "finished",
    ]


def test_a_year_the_broker_has_nothing_for_does_not_fail_the_request(
    tmp_path: Path, journal: SpyJournal
) -> None:
    """⚠️ The case the screen's own suggested window produces on any recent instrument.

    Asking a symbol listed in 2021 for a window that starts in 2019 hands `backfill` two empty
    years. Refusing there would make the default unusable on exactly the symbols somebody has
    just discovered through the search — and the two years still count as done, because they
    are: there is nothing more to fetch for them.
    """
    source = EmptyBefore(utc(2021, 1, 1))

    outcome = collect(source, journal, tmp_path, date_from=utc(2019, 6, 1), date_to=utc(2021, 6, 1))

    assert journal.entries == [
        "started",
        "year 1",
        "year 2",
        "year 3",
        "catalogued EURUSD",
        "finished",
    ]
    assert outcome.slices_with_data == 1
    assert outcome.slices_total == 3
    assert journal.failure is None


def test_a_symbol_with_no_bars_anywhere_fails_with_a_sentence(
    tmp_path: Path, journal: SpyJournal
) -> None:
    """⚠️ And it fails rather than finishing quietly. Finishing would write a catalogue row
    claiming a dataset that is not on disk, and the backtest screen would then offer it."""
    outcome = collect(
        Barren(), journal, tmp_path, date_from=utc(2020, 1, 1), date_to=utc(2020, 6, 1)
    )

    assert journal.entries == ["started", "year 1", "failed"]
    assert outcome.found_nothing
    assert journal.failure is not None
    assert "EURUSD" in journal.failure
    assert "H1" in journal.failure
    assert journal.finished_candles is None, "a failed collection must not report a count"


def test_nothing_is_catalogued_when_nothing_was_collected(
    tmp_path: Path, journal: SpyJournal
) -> None:
    """The separating assertion: without it, a run that catalogued an empty dataset *and* then
    failed would pass every test above."""
    collect(Barren(), journal, tmp_path, date_from=utc(2020, 1, 1), date_to=utc(2020, 6, 1))

    assert "catalogued EURUSD" not in journal.entries
    assert journal.catalogued_coverage is None


def test_the_catalogued_coverage_describes_the_disk_not_the_request(
    tmp_path: Path, journal: SpyJournal
) -> None:
    """⚠️ The whole reason the coverage is read back from the files.

    The request asks for the first half of 2021 on a symbol listed in March. The catalogue must
    claim what is there — from March — and not what was asked for, or the backtest screen would
    offer two months of window with no bars behind them.
    """
    source = EmptyBefore(utc(2021, 3, 1))

    collect(source, journal, tmp_path, date_from=utc(2021, 1, 1), date_to=utc(2021, 6, 1))

    assert journal.catalogued_coverage is not None
    assert journal.catalogued_coverage.date_from >= utc(2021, 3, 1)


def test_the_catalogued_spread_is_the_last_quote_of_the_run(
    tmp_path: Path, journal: SpyJournal
) -> None:
    """⚠️ A spread is a **quote**, not a property of the symbol.

    Every slice asks the source for one, and they are all measured while the collection runs —
    so the last is the most recent, and the most recent is the one a backtest should be charged
    at. Taking the first would file the run under whatever the book looked like minutes earlier,
    which on this desk is the difference between 11 points and 1 (see the spread measured with
    the market shut in `docs/aulas/PR-226-o-spread-do-instrumento.md`).

    This is also the only scenario in the file where the spread is anything but `None`, which is
    what lets it notice the quote being dropped on the way to the catalogue.
    """
    source = QuotesASpread([Decimal("11"), Decimal("1")])

    collect(source, journal, tmp_path, date_from=utc(2020, 11, 1), date_to=utc(2021, 2, 1))

    assert source.asked == 2, "one quote per slice with data — two years, two quotes"
    assert journal.catalogued_spread == Decimal("1")


def test_a_second_disjoint_collection_widens_the_catalogue_rather_than_moving_it(
    tmp_path: Path, journal: SpyJournal
) -> None:
    """⚠️ The failure this PR exists to avoid, stated at the level somebody would hit it.

    Collect 2021, then collect 2019. `write_candles` leaves 2021 exactly where it was, so the
    catalogue has to keep claiming it — a row rebuilt from the second run's own candles would
    say the symbol covers 2019 alone, and three minutes of downloaded bars would stop being
    offered to a backtest without anything being deleted.
    """
    collect(
        SyntheticSource(), journal, tmp_path, date_from=utc(2021, 3, 1), date_to=utc(2021, 3, 5)
    )

    second = SpyJournal()
    outcome = collect(
        SyntheticSource(), second, tmp_path, date_from=utc(2019, 3, 1), date_to=utc(2019, 3, 5)
    )

    assert second.catalogued_coverage is not None
    assert second.catalogued_coverage.date_from < utc(2020)
    assert second.catalogued_coverage.date_to > utc(2021), "the earlier year must survive"

    first_count = journal.finished_candles
    assert first_count is not None
    # ⚠️ The sum of the two runs, not twice either of them: the two windows are the same five
    # calendar days a year apart and hold different numbers of bars, because 1 March 2019 is a
    # Friday and 1 March 2021 is a Monday. A test that assumed symmetry here would be asserting
    # something about the calendar rather than about the catalogue.
    assert second.finished_candles == first_count + outcome.candles


def test_recollecting_the_same_range_reports_the_same_totals(
    tmp_path: Path, journal: SpyJournal
) -> None:
    """Idempotence, asserted through what the catalogue would claim rather than through the
    files: running it twice must leave the same numbers, or a nightly re-run doubles them."""
    collect(
        SyntheticSource(), journal, tmp_path, date_from=utc(2021, 3, 1), date_to=utc(2021, 3, 5)
    )

    again = SpyJournal()
    collect(SyntheticSource(), again, tmp_path, date_from=utc(2021, 3, 1), date_to=utc(2021, 3, 5))

    assert again.finished_candles == journal.finished_candles
    assert again.finished_gaps == journal.finished_gaps


def test_a_gap_across_new_year_is_counted(tmp_path: Path, journal: SpyJournal) -> None:
    """⚠️ The gap a per-slice count cannot see, and the reason gaps are counted at the end.

    Two collections either side of a New Year leave a hole that belongs to neither year. Summing
    each `BackfillReport.gaps` would report zero — every slice is internally continuous — and a
    holiday closure is the commonest gap there is.
    """
    collect(
        SyntheticSource(), journal, tmp_path, date_from=utc(2020, 12, 1), date_to=utc(2020, 12, 3)
    )
    collect(
        SyntheticSource(), journal, tmp_path, date_from=utc(2021, 2, 1), date_to=utc(2021, 2, 3)
    )

    assert journal.finished_gaps is not None
    assert journal.finished_gaps > 0, "the two months between the collections are a gap"


def test_a_backwards_range_is_refused_before_anything_is_written(
    tmp_path: Path, journal: SpyJournal
) -> None:
    """Refused by `year_slices`, before `started` is even recorded — so a row cannot be left
    saying `running` for a request that could never run."""
    with pytest.raises(ValueError, match="is before"):
        collect(SyntheticSource(), journal, tmp_path, date_from=utc(2022), date_to=utc(2021))

    assert journal.entries == []


class Breaks:
    """A source whose terminal drops the connection partway through a batch.

    Not a `LookupError`, which `run_collection` treats as an ordinary empty year — this is the
    class of failure that ends a collection: the terminal went away, the disk filled, the
    account logged out.
    """

    def __init__(self, *, after: int = 0) -> None:
        self._inner = SyntheticSource()
        self.calls = 0
        self._after = after

    def instrument(self, symbol: str) -> InstrumentSpec:
        return self._inner.instrument(symbol)

    def spread_points(self, symbol: str) -> Decimal | None:
        return self._inner.spread_points(symbol)

    def candles(
        self, symbol: str, timeframe: str, start: dt.datetime, end: dt.datetime
    ) -> list[Candle]:
        self.calls += 1
        if self.calls > self._after:
            raise ConnectionError("the terminal went away")
        return self._inner.candles(symbol, timeframe, start, end)


def test_one_collection_failing_leaves_the_others_untouched(tmp_path: Path) -> None:
    """⚠️ **The independence a batch is built on (CA-07 of the multi-symbol feature).**

    A batch of N symbols is N rows and N jobs sharing one Parquet root and one database. Nothing
    links them, which is the design — but "nothing links them" is a claim about *shared state*,
    and shared state is where it would be false. A failure partway through the second symbol must
    not damage the first symbol's partitions or stop the third from being written.

    ⚠️ What this does **not** prove is that the agent records the failure on the row and moves to
    the next job — that lives in `collect_range`, which imports MetaTrader and therefore cannot
    run on this CI. What it proves is the half that could actually go wrong silently: the data.
    """
    window = (utc(2021), utc(2021, 12, 31))
    first, third = SpyJournal(), SpyJournal()

    run_collection(
        SyntheticSource(),
        first,
        root=tmp_path,
        symbol="EURUSD",
        timeframe="H1",
        date_from=window[0],
        date_to=window[1],
    )
    with pytest.raises(ConnectionError):
        run_collection(
            Breaks(),
            SpyJournal(),
            root=tmp_path,
            symbol="AAPL",
            timeframe="H1",
            date_from=window[0],
            date_to=window[1],
        )
    run_collection(
        SyntheticSource(),
        third,
        root=tmp_path,
        symbol="GBPUSD",
        timeframe="H1",
        date_from=window[0],
        date_to=window[1],
    )

    # The two that ran are both finished and both catalogued, and the one in the middle wrote
    # nothing that either of them can see.
    assert first.entries[-1] == "finished"
    assert third.entries[-1] == "finished"
    assert first.finished_candles is not None
    assert first.finished_candles > 0
    assert third.finished_candles is not None
    assert third.finished_candles > 0

    # ⚠️ Asserted on the **disk**, not on the journals. A journal reports what the code believed;
    # the partitions are what a later backtest actually reads, and a failure that corrupted them
    # would leave both journals saying "finished" regardless.
    written = {path.parent.parent.parent.name for path in tmp_path.rglob("*.parquet")}
    assert written == {"symbol=EURUSD", "symbol=GBPUSD"}


def test_a_source_that_breaks_midway_does_not_catalogue_a_partial_series(tmp_path: Path) -> None:
    """⚠️ The half-written case, which is the one that would lie.

    The connection survives the first year and dies in the second. Nothing may reach the
    catalogue: a `datasets` row claiming a range whose later half is missing is worse than no
    row at all, because the next backtest would read it as coverage and silently test less
    history than it thinks.
    """
    spy = SpyJournal()

    with pytest.raises(ConnectionError):
        run_collection(
            Breaks(after=1),
            spy,
            root=tmp_path,
            symbol="EURUSD",
            timeframe="H1",
            date_from=utc(2020),
            date_to=utc(2021, 12, 31),
        )

    assert "catalogued EURUSD" not in spy.entries
    assert spy.finished_candles is None

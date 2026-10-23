"""Cutting a collection range, and reading back what the disk holds afterwards."""

import datetime as dt
from decimal import Decimal
from itertools import pairwise
from pathlib import Path

import pytest

from tradeforge_collector.collect import CollectionOutcome, year_slices
from tradeforge_collector.storage import coverage, write_candles
from tradeforge_engine.domain import Candle


def utc(year: int, month: int = 1, day: int = 1, hour: int = 0) -> dt.datetime:
    return dt.datetime(year, month, day, hour, tzinfo=dt.UTC)


class TestCuttingTheRange:
    def test_a_range_inside_one_year_is_one_slice(self) -> None:
        found = year_slices(utc(2021, 3, 1), utc(2021, 9, 30))

        assert found == [(utc(2021, 3, 1), utc(2021, 9, 30))]

    def test_a_range_across_years_is_cut_at_the_boundaries(self) -> None:
        found = year_slices(utc(2020, 6, 15), utc(2022, 3, 1))

        assert found == [
            (utc(2020, 6, 15), dt.datetime(2020, 12, 31, 23, 59, 59, 999999, tzinfo=dt.UTC)),
            (utc(2021), dt.datetime(2021, 12, 31, 23, 59, 59, 999999, tzinfo=dt.UTC)),
            (utc(2022), utc(2022, 3, 1)),
        ]

    def test_the_slices_touch_without_overlapping(self) -> None:
        """⚠️ The invariant, and its failure is a *missing* year rather than a duplicate one.

        `write_candles` replaces whole year partitions, so a bar handed to two slices lands in
        one partition and the second write erases what the first put there. An off-by-one that
        made the slices overlap would therefore show up as a year that vanished — and only for
        ranges that happen to straddle a boundary.
        """
        found = year_slices(utc(2019, 5, 1), utc(2023, 2, 2))

        for (_, earlier_end), (later_start, _) in pairwise(found):
            assert later_start > earlier_end, "slices must not overlap"
            assert later_start - earlier_end == dt.timedelta(microseconds=1), "or leave a hole"

    def test_a_range_that_ends_where_it_starts_is_still_one_slice(self) -> None:
        moment = utc(2024, 7, 4, 13)

        assert year_slices(moment, moment) == [(moment, moment)]

    def test_a_range_ending_on_new_year_s_eve_does_not_produce_an_empty_next_slice(self) -> None:
        """⚠️ The off-by-one that separates `<=` from `<` in the loop.

        Ending exactly at the last instant of a year must not roll the cursor into January and
        emit a slice with nothing in it — a slice the collector would then have to download,
        find empty, and count against the "found nothing at all" verdict.
        """
        last = dt.datetime(2021, 12, 31, 23, 59, 59, 999999, tzinfo=dt.UTC)

        found = year_slices(utc(2021, 12, 1), last)

        assert found == [(utc(2021, 12, 1), last)]

    def test_a_backwards_range_raises_rather_than_returning_nothing(self) -> None:
        """Empty would be indistinguishable from a range that simply holds no data."""
        with pytest.raises(ValueError, match="is before"):
            year_slices(utc(2022), utc(2021))

    def test_the_slices_keep_the_timezone_they_were_given(self) -> None:
        """A naive boundary against aware bounds raises `TypeError` on the first comparison —
        but only for a range long enough to reach a boundary, which a test of one year misses."""
        found = year_slices(utc(2020, 6, 1), utc(2021, 6, 1))

        assert all(start.tzinfo is dt.UTC and end.tzinfo is dt.UTC for start, end in found)


class TestWhatTheCollectionFound:
    def test_every_slice_empty_is_the_failure(self) -> None:
        assert CollectionOutcome(candles=0, slices_with_data=0, slices_total=3).found_nothing

    def test_one_slice_with_data_is_enough(self) -> None:
        """⚠️ A symbol listed in 2018 and asked for from 2015 has three empty years, and the
        screen's own suggested window is what asks for them. Refusing here would make the
        default unusable on every recently listed instrument."""
        outcome = CollectionOutcome(candles=6150, slices_with_data=1, slices_total=4)

        assert not outcome.found_nothing

    def test_a_range_with_no_slices_at_all_reports_nothing_found(self) -> None:
        assert CollectionOutcome(candles=0, slices_with_data=0, slices_total=0).found_nothing


def a_candle(when: dt.datetime) -> Candle:
    return Candle(
        time=when,
        open=Decimal("1.1000"),
        high=Decimal("1.1010"),
        low=Decimal("1.0990"),
        close=Decimal("1.1005"),
        tick_volume=100,
        spread=1,
        real_volume=0,
    )


class TestCoverageComesFromTheDisk:
    def test_it_reports_the_span_and_the_count_of_what_was_written(self, tmp_path: Path) -> None:
        write_candles(
            tmp_path, "EURUSD", "H1", [a_candle(utc(2021, 3, 1)), a_candle(utc(2021, 3, 2))]
        )

        found = coverage(tmp_path, "EURUSD", "H1")

        assert found is not None
        assert found.date_from == utc(2021, 3, 1)
        assert found.date_to == utc(2021, 3, 2)
        assert found.candle_count == 2

    def test_a_later_disjoint_collection_does_not_shrink_the_coverage(self, tmp_path: Path) -> None:
        """⚠️ The whole reason this is read from the disk instead of from the run that just
        finished.

        `write_candles` only deletes the year partitions it is about to write, so 2020 survives
        a later collection of 2015 untouched. A catalogue row built from the second run's own
        candles would claim the symbol covers 2015 alone — and the screen believes the row, so
        bars somebody waited minutes to download would stop being offered to a backtest.
        """
        write_candles(tmp_path, "EURUSD", "H1", [a_candle(utc(2020, 6, 1))])
        write_candles(tmp_path, "EURUSD", "H1", [a_candle(utc(2015, 2, 1))])

        found = coverage(tmp_path, "EURUSD", "H1")

        assert found is not None
        assert found.date_from == utc(2015, 2, 1)
        assert found.date_to == utc(2020, 6, 1), "the untouched year must still be claimed"
        assert found.candle_count == 2

    def test_recollecting_the_same_year_replaces_rather_than_appends(self, tmp_path: Path) -> None:
        """The idempotence the catalogue depends on, asserted through the reader that now
        derives it: two runs over the same year leave one year's worth of bars."""
        write_candles(
            tmp_path, "EURUSD", "H1", [a_candle(utc(2021, 3, 1)), a_candle(utc(2021, 3, 2))]
        )
        write_candles(
            tmp_path, "EURUSD", "H1", [a_candle(utc(2021, 3, 1)), a_candle(utc(2021, 3, 2))]
        )

        found = coverage(tmp_path, "EURUSD", "H1")

        assert found is not None
        assert found.candle_count == 2

    def test_a_symbol_never_collected_has_no_coverage(self, tmp_path: Path) -> None:
        """`None`, not a zero-length span. Zero would be a claim about a dataset that does not
        exist, and the catalogue would carry two invented instants."""
        assert coverage(tmp_path, "EURUSD", "H1") is None

    def test_a_directory_that_exists_and_holds_nothing_has_no_coverage(
        self, tmp_path: Path
    ) -> None:
        """⚠️ Separates "no directory" from "empty directory", which an `exists()` check alone
        would not. An interrupted write can leave the second, and reporting a span for it would
        mean asking Arrow for the minimum of nothing."""
        (tmp_path / "symbol=EURUSD" / "timeframe=H1").mkdir(parents=True)

        assert coverage(tmp_path, "EURUSD", "H1") is None

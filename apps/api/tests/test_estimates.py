"""The arithmetic of a time estimate, with no database: bars on the calendar, years in a plan, and
a rate chosen by timeframe with the pooled one behind it."""

import datetime as dt

import pytest

from tradeforge_api.estimates import calendar_bars, collection_time, planned_years


def at(year: int, month: int = 1, day: int = 1) -> dt.datetime:
    return dt.datetime(year, month, day, tzinfo=dt.UTC)


class TestCalendarBars:
    def test_a_day_of_h1_is_twenty_four_bars_closures_included(self) -> None:
        # Saturday 2024-01-06: no market trades, and the calendar still counts it — the same way
        # for the runs measured and the runs asked about, which is why it cancels.
        assert calendar_bars(at(2024, 1, 6), at(2024, 1, 7), "H1") == 24

    def test_the_timeframe_sets_the_count(self) -> None:
        assert calendar_bars(at(2024, 1, 1), at(2024, 1, 2), "M15") == 96
        assert calendar_bars(at(2024, 1, 1), at(2024, 1, 2), "D1") == 1

    def test_a_backwards_window_holds_nothing_rather_than_negative_bars(self) -> None:
        # A negative count would turn a rate negative and an estimate into a refund.
        assert calendar_bars(at(2024, 1, 2), at(2024, 1, 1), "H1") == 0

    def test_an_unknown_timeframe_is_refused_not_guessed(self) -> None:
        with pytest.raises(ValueError, match="unknown timeframe"):
            calendar_bars(at(2024), at(2025), "H2")


class TestPlannedYears:
    def test_a_window_counts_every_calendar_year_it_touches(self) -> None:
        # The agent downloads by calendar year (`year_slices`): 2019 to 2021 is three slices.
        assert planned_years([(at(2019), at(2021, 12, 31))]) == 3

    def test_two_windows_add_up(self) -> None:
        assert planned_years([(at(2010), at(2011, 12, 31)), (at(2024), at(2024, 6, 1))]) == 3

    def test_no_window_is_no_year(self) -> None:
        assert planned_years([]) == 0


class TestCollectionTime:
    RATES = ({"M15": (120.0, 7), "H1": (30.0, 12)}, (60.0, 19))

    def test_the_timeframe_s_own_rate_is_used_when_there_is_one(self) -> None:
        estimate = collection_time(self.RATES, "M15", 3)
        assert estimate is not None
        assert (estimate.seconds, estimate.based_on) == (360.0, 7)

    def test_a_timeframe_never_downloaded_falls_back_to_the_pooled_rate(self) -> None:
        estimate = collection_time(self.RATES, "H4", 2)
        assert estimate is not None
        assert (estimate.seconds, estimate.based_on) == (120.0, 19)

    def test_no_history_is_no_estimate_not_zero(self) -> None:
        # Zero would read as "instant"; the truth is "nothing to measure by".
        assert collection_time(None, "H1", 5) is None

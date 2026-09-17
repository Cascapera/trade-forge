"""What a window still needs collected, on hand-worked dates.

Every expected window below is derived in the comment beside it, not read off the code. The
three rules those comments lean on are the module's: a window to collect is made of **whole
calendar years** (a partial year would erase the rest of that year's partition), it **reaches
the data already on disk** (a detached window would leave a hole the index cannot see), and an
edge within **four days plus one bar** of the request is not a gap (weekends and holidays).
"""

import datetime as dt

from tradeforge_api.collection_plan import EDGE_SLACK, Window, missing_windows

H1 = dt.timedelta(hours=1)
W1 = dt.timedelta(weeks=1)
NOW = dt.datetime(2026, 9, 17, 12, 0, tzinfo=dt.UTC)  # a Thursday


def at(year: int, month: int = 1, day: int = 1, hour: int = 0) -> dt.datetime:
    return dt.datetime(year, month, day, hour, tzinfo=dt.UTC)


def year_end(year: int) -> dt.datetime:
    return dt.datetime(year, 12, 31, 23, 59, 59, 999999, tzinfo=dt.UTC)


def whole(first: int, last: int) -> Window:
    return Window(at(first), year_end(last))


# Shaped like this project's real EURUSD index (measured on M15 and H4): asked from 2020-01-01,
# the first bar is on the 2nd.
ON_DISK = Window(at(2020, 1, 2, 21), at(2026, 9, 10, 20))


def plan(  # noqa: PLR0913 — one keyword per fact the plan is computed from
    date_from: dt.datetime,
    date_to: dt.datetime,
    *,
    on_disk: Window | None = ON_DISK,
    oldest: dt.datetime | None = None,
    now: dt.datetime = NOW,
    bar: dt.timedelta = H1,
) -> list[Window]:
    return missing_windows(
        date_from=date_from, date_to=date_to, on_disk=on_disk, oldest=oldest, now=now, bar=bar
    )


class TestNothingOnDisk:
    def test_the_whole_window_is_collected_in_whole_years(self) -> None:
        # Asked 2023-03-10 .. 2024-06-30 → years 2023 and 2024, whole.
        assert plan(at(2023, 3, 10), at(2024, 6, 30), on_disk=None) == [whole(2023, 2024)]

    def test_the_current_year_stops_at_now(self) -> None:
        # Asked 2026-02-01 .. 2026-12-31, now 2026-09-17 12:00 → 2026-01-01 .. now.
        assert plan(at(2026, 2), at(2026, 12, 31), on_disk=None) == [Window(at(2026), NOW)]


class TestInsideWhatIsOnDisk:
    def test_a_window_inside_the_data_needs_nothing(self) -> None:
        assert plan(at(2021), at(2025, 12, 31)) == []

    def test_a_holiday_at_the_start_is_not_a_gap(self) -> None:
        # Asked from 2020-01-01 00:00; the first bar is 2020-01-02 21:00, 45 hours later — well
        # inside four days and one bar. Without the slack, every request from New Year's Day
        # would re-collect 2020 for ever, because no bar can ever be stamped on it.
        assert plan(at(2020), at(2025, 12, 31)) == []

    def test_a_weekend_at_the_end_is_not_a_gap(self) -> None:
        # Saturday 2026-09-12 10:00; the last bar is Friday 2026-09-11 20:00, 14 hours back.
        # The request runs to the end of the year, but nothing after now can be collected.
        saturday = at(2026, 9, 12, 10)
        on_disk = Window(at(2020, 1, 2, 21), at(2026, 9, 11, 20))
        assert plan(at(2021), at(2026, 12, 31), on_disk=on_disk, now=saturday) == []


class TestBeforeWhatIsOnDisk:
    def test_the_missing_years_include_the_first_year_on_disk(self) -> None:
        # Asked from 2019-06-01; the data starts 2020-01-02 → years 2019 .. 2020. 2020 is
        # included because the rule does not guess whether its partition is complete.
        assert plan(at(2019, 6), at(2021)) == [whole(2019, 2020)]

    def test_a_window_far_before_the_data_is_joined_to_it(self) -> None:
        # Asked 2015-01-05 .. 2016-12-30; the data starts 2020 → 2015 .. 2020, not 2015 .. 2016.
        # Collected alone, 2015-2016 would make the index read "2015 to 2026" over a hole in
        # 2017-2019, and a later request for 2018 would be told it is covered.
        assert plan(at(2015, 1, 5), at(2016, 12, 30)) == [whole(2015, 2020)]

    def test_just_past_the_slack_is_a_gap(self) -> None:
        # The first bar 4 days + 1 bar + 1 hour after the start of the request.
        on_disk = Window(at(2020, 1, 5, 2), at(2026, 9, 10, 20))
        assert plan(at(2020, 1, 1), at(2021), on_disk=on_disk) == [whole(2020, 2020)]

    def test_exactly_the_slack_is_not_a_gap(self) -> None:
        # The first bar 4 days + 1 bar after the start: still an edge, not a hole.
        on_disk = Window(at(2020, 1, 1) + EDGE_SLACK + H1, at(2026, 9, 10, 20))
        assert plan(at(2020, 1, 1), at(2021), on_disk=on_disk) == []

    def test_a_request_that_ends_before_the_first_bar_is_a_gap_however_close(self) -> None:
        # The data starts Monday 2020-01-06; asked Thursday 2020-01-02 .. Friday 2020-01-03.
        # Inside the slack, but the request touches no bar at all → 2020.
        on_disk = Window(at(2020, 1, 6), at(2026, 9, 10, 20))
        assert plan(at(2020, 1, 2), at(2020, 1, 3), on_disk=on_disk) == [whole(2020, 2020)]


class TestAfterWhatIsOnDisk:
    def test_the_missing_years_run_from_the_last_year_on_disk_to_now(self) -> None:
        # The data ends 2026-09-10 20:00, a week before now → 2026-01-01 .. now.
        assert plan(at(2025), at(2026, 12, 31)) == [Window(at(2026), NOW)]

    def test_a_window_far_after_the_data_is_joined_to_it(self) -> None:
        # Data 2018 .. 2019-06-30; asked 2024 → 2019 .. 2024.
        on_disk = Window(at(2018, 1, 2), at(2019, 6, 30))
        assert plan(at(2024, 2), at(2024, 3), on_disk=on_disk) == [whole(2019, 2024)]

    def test_exactly_the_slack_is_not_a_gap(self) -> None:
        # The request ends 4 days + 1 bar after the last bar: still an edge.
        last = at(2024, 6, 3, 20)
        on_disk = Window(at(2020, 1, 2), last)
        assert plan(at(2021), last + EDGE_SLACK + H1, on_disk=on_disk) == []

    def test_just_past_the_slack_is_a_gap(self) -> None:
        last = at(2024, 6, 3, 20)
        on_disk = Window(at(2020, 1, 2), last)
        assert plan(at(2021), last + EDGE_SLACK + 2 * H1, on_disk=on_disk) == [whole(2024, 2024)]

    def test_a_request_that_starts_after_the_last_bar_is_a_gap_however_close(self) -> None:
        # ⚠️ The case the slack alone got wrong: the data ends Friday 2026-09-11 20:00 and the
        # request is Monday 2026-09-14 .. Tuesday 2026-09-15, both inside four days. Forgiven as
        # an edge, the run would have read no bar at all → 2026-01-01 .. now.
        on_disk = Window(at(2020, 1, 2), at(2026, 9, 11, 20))
        assert plan(at(2026, 9, 14), at(2026, 9, 15), on_disk=on_disk) == [Window(at(2026), NOW)]

    def test_a_request_inside_the_last_bar_is_not_a_gap(self) -> None:
        # The last H1 bar opens 20:00 and runs to 21:00; a request from 20:30 reads it.
        on_disk = Window(at(2020, 1, 2), at(2024, 6, 3, 20))
        assert plan(at(2024, 6, 3, 20) + H1 / 2, at(2024, 6, 4), on_disk=on_disk) == []

    def test_the_slack_grows_with_the_bar(self) -> None:
        # The last weekly bar is stamped Sunday 2026-09-06 and now is 2026-09-16, 10 days on.
        # For W1 the slack is 4 days + 7 days = 11 days: no gap. For H1, 4 days + 1 hour: a gap.
        on_disk = Window(at(2020, 1, 5), at(2026, 9, 6))
        now = at(2026, 9, 16)
        assert plan(at(2021), at(2026, 12, 31), on_disk=on_disk, now=now, bar=W1) == []
        assert plan(at(2021), at(2026, 12, 31), on_disk=on_disk, now=now, bar=H1) == [
            Window(at(2026), now)
        ]


class TestBothSides:
    def test_both_gaps_in_the_same_years_are_one_window(self) -> None:
        # Data 2024-03-01 .. 2024-09-01; asked the whole of 2024 → 2024 once, not twice.
        on_disk = Window(at(2024, 3), at(2024, 9))
        assert plan(at(2024), year_end(2024), on_disk=on_disk) == [whole(2024, 2024)]

    def test_adjacent_gaps_are_one_window(self) -> None:
        # Data 2021-06 .. 2022-06; asked 2019 .. 2024 → 2019-2021 and 2022-2024 touch → one.
        on_disk = Window(at(2021, 6), at(2022, 6))
        assert plan(at(2019), at(2024, 6), on_disk=on_disk) == [whole(2019, 2024)]

    def test_distant_gaps_stay_two_windows(self) -> None:
        # Data 2020-01-02 .. 2025-06-30; asked 2018 .. now → 2018-2020 and 2025-now. Merging them
        # would download 2021-2024 again for nothing.
        on_disk = Window(at(2020, 1, 2), at(2025, 6, 30))
        assert plan(at(2018), at(2026, 12, 31), on_disk=on_disk) == [
            whole(2018, 2020),
            Window(at(2025), NOW),
        ]

    def test_gaps_a_year_apart_stay_two_windows(self) -> None:
        # Data 2020-06 .. 2022-06; asked 2018 .. 2024 → 2018-2020 and 2022-2024. 2021 lies
        # between them, on disk, and is not downloaded again.
        on_disk = Window(at(2020, 6), at(2022, 6))
        assert plan(at(2018), at(2024, 6), on_disk=on_disk) == [
            whole(2018, 2020),
            whole(2022, 2024),
        ]


class TestTimezones:
    def test_a_start_east_of_utc_counts_the_utc_year(self) -> None:
        # 2022-01-01 00:00 at +03:00 is 2021-12-31 21:00 UTC, a trading hour of 2021 → 2021-2022.
        east = dt.timezone(dt.timedelta(hours=3))
        start = dt.datetime(2022, 1, 1, tzinfo=east)
        assert plan(start, at(2022, 6), on_disk=None) == [whole(2021, 2022)]

    def test_an_end_west_of_utc_counts_the_utc_year(self) -> None:
        # 2021-12-31 22:00 at -03:00 is 2022-01-01 01:00 UTC → 2021-2022.
        west = dt.timezone(dt.timedelta(hours=-3))
        end = dt.datetime(2021, 12, 31, 22, tzinfo=west)
        assert plan(at(2021, 6), end, on_disk=None) == [whole(2021, 2022)]


class TestTheBrokersOldestBar:
    def test_nothing_before_the_oldest_bar_is_asked_for(self) -> None:
        # BTCUSD H1's oldest bar here is 2022-05-10 13:00. Asked 2020 .. 2023, nothing on disk
        # → from 2022 (whole year), not from 2020.
        oldest = at(2022, 5, 10, 13)
        assert plan(at(2020), at(2023, 6), on_disk=None, oldest=oldest) == [whole(2022, 2023)]

    def test_data_that_starts_at_the_oldest_bar_is_complete(self) -> None:
        # ⚠️ The case that would loop: the disk already starts where the broker does, so a
        # request from 2020 can never be satisfied by collecting again. Without the oldest bar,
        # every run from 2020 would queue the same useless collection.
        oldest = at(2022, 5, 10, 13)
        on_disk = Window(oldest, at(2026, 9, 10, 20))
        assert plan(at(2020), at(2025), on_disk=on_disk, oldest=oldest) == []

    def test_a_window_entirely_before_the_oldest_bar_needs_nothing(self) -> None:
        assert plan(at(2019), at(2020), on_disk=None, oldest=at(2022, 5, 10)) == []


def test_a_window_entirely_in_the_future_needs_nothing() -> None:
    assert plan(at(2027), at(2027, 6), on_disk=None) == []

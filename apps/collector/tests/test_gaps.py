"""Gap detection — the difference between a hole and a closed market.

A missing hour on a Tuesday is a strategy that traded a market it never saw. A missing
Saturday is Saturday. The whole value of this module is telling those two apart, so the
tests state exactly where the line falls.
"""

import datetime as dt

from tradeforge_collector.gaps import Gap, anomalies, find_gaps, format_report

H1 = dt.timedelta(hours=1)
D1 = dt.timedelta(days=1)


def at(day: int, hour: int = 0) -> dt.datetime:
    """A moment in January 2024. The 1st was a Monday; the 6th and 7th are the weekend."""
    return dt.datetime(2024, 1, day, hour, tzinfo=dt.UTC)


def test_a_continuous_series_has_no_gaps() -> None:
    times = [at(2, hour) for hour in range(24)]

    assert find_gaps(times, H1) == []


def test_a_missing_hour_on_a_weekday_is_anomalous() -> None:
    """The one that matters: an hour of history the strategy silently never saw."""
    times = [at(2, 9), at(2, 10), at(2, 12)]

    gaps = find_gaps(times, H1)

    assert len(gaps) == 1
    assert gaps[0].kind == "anomalous"
    assert gaps[0].missing == 1
    assert gaps[0].after == at(2, 10)
    assert gaps[0].before == at(2, 12)


def test_the_weekend_is_not_a_gap_worth_reading() -> None:
    """Friday's last bar to Monday's first: about fifty of these in a year of forex."""
    times = [at(5, 22), at(8, 0)]

    gaps = find_gaps(times, H1)

    assert len(gaps) == 1
    assert gaps[0].kind == "weekend"
    assert anomalies(gaps) == []


def test_the_friday_evening_close_counts_as_part_of_the_weekend() -> None:
    """Forex stops around 21:00 UTC on Friday — those last bars are missing legitimately.

    A rule of "every missing bar is a Saturday or a Sunday" would file every single
    weekend in the dataset as an anomaly, which is the same as having no report at all.
    """
    times = [at(5, 20), at(8, 0)]

    assert find_gaps(times, H1)[0].kind == "weekend"


def test_a_daily_weekend_gap_is_still_a_weekend() -> None:
    """Exactly 72 hours — the reason the rule is a window and not a duration ceiling.

    Any "shorter than three days" test would have to call this anomalous, and any
    "shorter than four days" test would have to call Christmas normal.
    """
    times = [at(5), at(8)]

    assert find_gaps(times, D1)[0].kind == "weekend"


def test_a_holiday_on_a_monday_pokes_out_of_the_window_and_is_flagged() -> None:
    """Christmas 2023 fell on a Monday: the market was shut Friday *through Monday*.

    By design this is anomalous. A holiday calendar would have to be maintained per
    country and per year, and would go stale in silence; a Monday will always be a
    Monday. You look at it once, decide it is fine, and move on.
    """
    times = [
        dt.datetime(2023, 12, 22, 22, tzinfo=dt.UTC),
        dt.datetime(2023, 12, 26, 0, tzinfo=dt.UTC),
    ]

    assert find_gaps(times, H1)[0].kind == "anomalous"


def test_a_week_long_outage_is_anomalous_even_though_it_spans_a_weekend() -> None:
    times = [at(4, 12), at(12, 12)]

    assert find_gaps(times, H1)[0].kind == "anomalous"


def test_one_stray_bar_outside_the_window_is_enough_to_flag_the_gap() -> None:
    """A weekend that starts an hour too early is a weekend plus a missing hour."""
    times = [at(5, 18), at(8, 0)]

    assert find_gaps(times, H1)[0].kind == "anomalous"


def test_the_report_counts_weekends_and_lists_anomalies() -> None:
    gaps = [
        Gap(after=at(5, 22), before=at(8, 0), missing=49, kind="weekend"),
        Gap(after=at(2, 10), before=at(2, 12), missing=1, kind="anomalous"),
    ]

    report = format_report(gaps)

    assert "1 weekend, 1 anomalous" in report
    assert "2024-01-02 10:00 -> 2024-01-02 12:00" in report


def test_the_report_says_when_it_truncates() -> None:
    """A report that silently shows 20 of 400 gaps reads as good news. It is not."""
    gaps = [
        Gap(after=at(day, 10), before=at(day, 12), missing=1, kind="anomalous")
        for day in range(1, 31)
    ]

    report = format_report(gaps, limit=5)

    assert "and 25 more anomalous gaps" in report

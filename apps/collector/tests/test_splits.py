"""Share splits: the arithmetic by hand, the files adjusted once, and the gap a split leaves."""

import argparse
import datetime as dt
import math
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from tradeforge_collector.backfill import backfill
from tradeforge_collector.cli import _day, _ratio
from tradeforge_collector.splits import (
    Split,
    adjust,
    applied_splits,
    bring_up_to_date,
    find_candidates,
    observed_gap,
    refuse_unless_the_bars_split,
    shows_the_split,
)
from tradeforge_collector.storage import read_candles, write_candles
from tradeforge_collector.synthetic import SyntheticSource
from tradeforge_db.splits import RecordedSplit
from tradeforge_engine.domain import Candle, InstrumentSpec

THREE_HOURS = dt.timedelta(hours=3)


def utc(year: int, month: int, day: int, hour: int = 0) -> dt.datetime:
    return dt.datetime(year, month, day, hour, tzinfo=dt.UTC)


def bar(moment: dt.datetime, price: str, *, spread: int = 30, real_volume: int = 100) -> Candle:
    """A flat bar at `price`, with a range a split must scale with it."""
    level = Decimal(price)
    return Candle(
        time=moment,
        open=level,
        high=level + Decimal(10),
        low=level - Decimal(10),
        close=level,
        tick_volume=500,
        spread=spread,
        real_volume=real_volume,
    )


def split(day: dt.date, after: int, before: int, offset: dt.timedelta = THREE_HOURS) -> Split:
    return Split.of(
        RecordedSplit(
            symbol="AMZN",
            effective_on=day,
            shares_after=after,
            shares_before=before,
            server_offset=offset,
        )
    )


AMZN = split(dt.date(2022, 6, 6), 20, 1)
"""AMZN's 20-for-1: first day at the new price Monday 6 June 2022, on a broker 3 h ahead of UTC."""


# --------------------------------------------------------------------------- #
# The instant and the key                                                       #
# --------------------------------------------------------------------------- #


def test_the_split_starts_at_midnight_on_the_broker_s_clock() -> None:
    """The daily bars of a broker at UTC+3 are stamped 21:00 UTC the day before: that is when
    6 June starts there, and the first bar at the new price is that one."""
    assert AMZN.effective_at == utc(2022, 6, 5, 21)
    assert AMZN.key == "2022-06-06=20:1"


# --------------------------------------------------------------------------- #
# The arithmetic, by hand                                                       #
# --------------------------------------------------------------------------- #


def test_a_bar_before_a_20_for_1_is_divided_by_20_and_one_after_is_not() -> None:
    before = bar(utc(2022, 6, 2, 21), "2446.92", spread=50, real_volume=100)
    after = bar(utc(2022, 6, 5, 21), "125.20")

    [adjusted, untouched] = adjust([before, after], [AMZN], timeframe="D1")

    assert adjusted.open == Decimal("122.346")
    assert adjusted.high == Decimal("122.846")  # 2456.92 / 20
    assert adjusted.low == Decimal("121.846")  # 2436.92 / 20
    assert adjusted.close == Decimal("122.346")
    assert adjusted.spread == 3  # 50 points / 20 = 2.5: half up, where half-even would say 2
    assert adjusted.real_volume == 2000  # shares: twenty times as many
    assert adjusted.tick_volume == 500  # ticks are not shares
    assert untouched is after


def test_two_splits_multiply_before_the_first_and_only_the_second_applies_between() -> None:
    """TSLA: 5-for-1 in August 2020, 3-for-1 in August 2022. A 2019 bar is divided by 15."""
    tsla = [split(dt.date(2020, 8, 31), 5, 1), split(dt.date(2022, 8, 25), 3, 1)]

    early, middle, late = adjust(
        [
            bar(utc(2019, 5, 1, 21), "1500"),
            bar(utc(2021, 5, 3, 21), "690"),
            bar(utc(2023, 5, 1, 21), "160"),
        ],
        tsla,
        timeframe="D1",
    )

    assert early.close == Decimal(100)
    assert middle.close == Decimal(230)
    assert late.close == Decimal(160)


def test_a_price_that_does_not_divide_keeps_the_storage_s_ten_places() -> None:
    [adjusted] = adjust(
        [bar(utc(2022, 8, 22, 21), "890.65")], [split(dt.date(2022, 8, 25), 3, 1)], timeframe="D1"
    )

    assert adjusted.close == Decimal("296.8833333333")


def test_a_reverse_split_multiplies() -> None:
    [adjusted] = adjust(
        [bar(utc(2020, 1, 6, 21), "15")], [split(dt.date(2020, 2, 3), 1, 10)], timeframe="D1"
    )

    assert adjusted.close == Decimal(150)
    assert adjusted.real_volume == 10  # ten times fewer shares


def test_no_split_leaves_the_bars_alone() -> None:
    bars = [bar(utc(2022, 6, 2, 21), "2446.92")]

    assert adjust(bars, [], timeframe="D1") == bars


@given(
    price=st.decimals(min_value=Decimal("0.01"), max_value=Decimal(5000), places=2),
    ratios=st.lists(st.sampled_from([(2, 1), (3, 1), (4, 1), (10, 1), (1, 5), (3, 2)]), max_size=3),
)
def test_splits_applied_one_at_a_time_land_where_all_at_once_do(
    price: Decimal, ratios: list[tuple[int, int]]
) -> None:
    """What `bring_up_to_date` relies on: a file adjusted for one split, then for the next, ends
    where a fresh download adjusted for both does — to the storage's last place, since each step
    rounds once."""
    splits = [
        split(dt.date(2015 + n, 3, 2), after, before) for n, (after, before) in enumerate(ratios)
    ]
    bars = [bar(utc(2014, 6, 2, 21), str(price + 20))]

    at_once = adjust(bars, splits, timeframe="D1")
    stepwise = bars
    for one in splits:
        stepwise = adjust(stepwise, [one], timeframe="D1")

    assert abs(stepwise[0].close - at_once[0].close) <= Decimal("1e-9")


# --------------------------------------------------------------------------- #
# The check: no gap, no split                                                   #
# --------------------------------------------------------------------------- #


def test_a_raw_series_shows_the_split_and_an_adjusted_one_does_not() -> None:
    raw = [bar(utc(2022, 6, 2, 21), "2446.92"), bar(utc(2022, 6, 5, 21), "125.20")]
    already_adjusted = [bar(utc(2022, 6, 2, 21), "122.35"), bar(utc(2022, 6, 5, 21), "125.20")]

    raw_gap = observed_gap(raw, AMZN.effective_at, "D1")
    adjusted_gap = observed_gap(already_adjusted, AMZN.effective_at, "D1")

    assert raw_gap is not None
    assert shows_the_split(raw_gap, AMZN)
    assert adjusted_gap is not None
    assert not shows_the_split(adjusted_gap, AMZN)


def test_bars_already_adjusted_are_refused_rather_than_divided_again() -> None:
    """XP's Brazilian shares come adjusted: dividing them again would invent the crash."""
    already_adjusted = [bar(utc(2022, 6, 2, 21), "122.35"), bar(utc(2022, 6, 5, 21), "125.20")]

    with pytest.raises(ValueError, match="show no 20-for-1 gap"):
        refuse_unless_the_bars_split(already_adjusted, [AMZN], timeframe="D1", where="AMZN D1")


def test_bars_that_do_not_straddle_the_split_cannot_be_checked_and_pass() -> None:
    one_side = [bar(utc(2021, 6, 2, 21), "3200"), bar(utc(2021, 6, 3, 21), "3190")]

    assert observed_gap(one_side, AMZN.effective_at, "D1") is None
    refuse_unless_the_bars_split(one_side, [AMZN], timeframe="D1", where="AMZN D1")


# --------------------------------------------------------------------------- #
# Files: adjusted once, whatever runs again                                     #
# --------------------------------------------------------------------------- #


def two_years_raw(root: Path, timeframe: str = "D1") -> None:
    write_candles(
        root,
        "AMZN",
        timeframe,
        [
            bar(utc(2021, 12, 30, 21), "3300"),
            bar(utc(2022, 6, 2, 21), "2446.92"),
            bar(utc(2022, 6, 5, 21), "125.20"),
        ],
    )
    write_candles(root, "AMZN", timeframe, [bar(utc(2023, 1, 3, 21), "85.82")])


def year_directory(root: Path, year: int, timeframe: str = "D1") -> Path:
    return root / "symbol=AMZN" / f"timeframe={timeframe}" / f"year={year}"


def test_a_raw_file_carries_no_stamp_and_an_adjusted_one_names_its_splits(tmp_path: Path) -> None:
    two_years_raw(tmp_path)

    assert applied_splits(year_directory(tmp_path, 2022)) == frozenset()

    bring_up_to_date(tmp_path, "AMZN", [AMZN])

    assert applied_splits(year_directory(tmp_path, 2022)) == {"2022-06-06=20:1"}


def test_bringing_the_files_up_to_date_divides_every_timeframe_once(tmp_path: Path) -> None:
    two_years_raw(tmp_path, "D1")
    two_years_raw(tmp_path, "H1")

    rewritten = bring_up_to_date(tmp_path, "AMZN", [AMZN])
    again = bring_up_to_date(tmp_path, "AMZN", [AMZN])

    # 2021 and 2022 hold bars before the split; 2023 holds none and is not rewritten.
    assert {(part.timeframe, part.year) for part in rewritten} == {
        ("D1", 2021),
        ("D1", 2022),
        ("H1", 2021),
        ("H1", 2022),
    }
    assert again == []
    closes = [candle.close for candle in read_candles(tmp_path, "AMZN", "D1")]
    assert closes == [Decimal(165), Decimal("122.346"), Decimal("125.20"), Decimal("85.82")]


def test_a_year_after_every_split_is_not_touched(tmp_path: Path) -> None:
    """Rewriting it would change nothing but its modification time — and every remote worker
    downloads a file whose time moved."""
    two_years_raw(tmp_path)
    file_2023 = next(year_directory(tmp_path, 2023).glob("*.parquet"))
    stamp = file_2023.stat().st_mtime_ns

    bring_up_to_date(tmp_path, "AMZN", [AMZN])

    assert file_2023.stat().st_mtime_ns == stamp


def test_a_split_recorded_later_is_applied_alone(tmp_path: Path) -> None:
    """The first split is in the stamp already; only the new one divides."""
    later = split(dt.date(2022, 12, 1), 2, 1)
    two_years_raw(tmp_path)
    bring_up_to_date(tmp_path, "AMZN", [AMZN])

    bring_up_to_date(tmp_path, "AMZN", [AMZN, later])

    closes = [candle.close for candle in read_candles(tmp_path, "AMZN", "D1")]
    assert closes == [Decimal("82.5"), Decimal("61.173"), Decimal("62.6"), Decimal("85.82")]
    assert applied_splits(year_directory(tmp_path, 2022)) == {"2022-06-06=20:1", "2022-12-01=2:1"}


def test_files_that_disagree_inside_one_year_are_refused(tmp_path: Path) -> None:
    """Half a year divided and half not: no rewrite can reconcile it, so refused, not guessed."""
    two_years_raw(tmp_path)
    bring_up_to_date(tmp_path, "AMZN", [AMZN])
    write_candles(tmp_path / "raw", "AMZN", "D1", [bar(utc(2022, 1, 3, 21), "3400")])
    (year_directory(tmp_path, 2022) / "part-9.parquet").write_bytes(
        (year_directory(tmp_path / "raw", 2022) / "part-0.parquet").read_bytes()
    )

    with pytest.raises(ValueError, match="disagree"):
        applied_splits(year_directory(tmp_path, 2022))


# --------------------------------------------------------------------------- #
# A download is raw: written adjusted, or refused                               #
# --------------------------------------------------------------------------- #


class _ListedSource:
    """A broker that hands over exactly these bars, under a synthetic instrument's spec."""

    def __init__(self, candles: list[Candle]) -> None:
        self._candles = candles

    def instrument(self, symbol: str) -> InstrumentSpec:
        return SyntheticSource().instrument("EURUSD")

    def spread_points(self, symbol: str) -> Decimal | None:
        return None

    def candles(
        self, symbol: str, timeframe: str, start: dt.datetime, end: dt.datetime
    ) -> list[Candle]:
        return [candle for candle in self._candles if start <= candle.time <= end]


def test_a_download_across_a_split_is_written_adjusted_and_stamped(tmp_path: Path) -> None:
    source = _ListedSource(
        [bar(utc(2022, 6, 2, 21), "2446.92"), bar(utc(2022, 6, 5, 21), "125.20")]
    )

    backfill(
        source,  # type: ignore[arg-type]  # the protocol's other methods are never called here
        root=tmp_path,
        symbol="EURUSD",
        timeframe="D1",
        start=utc(2022, 1, 1),
        end=utc(2022, 12, 31),
        splits=[AMZN],
    )

    closes = [candle.close for candle in read_candles(tmp_path, "EURUSD", "D1")]
    assert closes == [Decimal("122.346"), Decimal("125.20")]
    directory = tmp_path / "symbol=EURUSD" / "timeframe=D1" / "year=2022"
    assert applied_splits(directory) == {"2022-06-06=20:1"}


def test_a_download_the_broker_already_adjusted_is_refused_and_nothing_written(
    tmp_path: Path,
) -> None:
    source = _ListedSource([bar(utc(2022, 6, 2, 21), "122.35"), bar(utc(2022, 6, 5, 21), "125.20")])

    with pytest.raises(ValueError, match="Already adjusted by the broker"):
        backfill(
            source,  # type: ignore[arg-type]
            root=tmp_path,
            symbol="EURUSD",
            timeframe="D1",
            start=utc(2022, 1, 1),
            end=utc(2022, 12, 31),
            splits=[AMZN],
        )

    assert not (tmp_path / "symbol=EURUSD").exists()


# --------------------------------------------------------------------------- #
# A week the split falls inside: rebuilt from its days, never scaled            #
# --------------------------------------------------------------------------- #


def day(moment: dt.datetime, opening: str, high: str, low: str, closing: str) -> Candle:
    return Candle(
        time=moment,
        open=Decimal(opening),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(closing),
        tick_volume=100,
        spread=1,
        real_volume=10,
    )


def week(moment: dt.datetime, opening: str, high: str, low: str, closing: str) -> Candle:
    return replace(day(moment, opening, high, low, closing), tick_volume=9999, spread=5)


SUNDAY_BEFORE = utc(2022, 5, 28, 21)
"""Weekly bars open on Sunday at the broker's midnight: Saturday 21:00 UTC at UTC+3."""
SPLIT_WEEK = utc(2022, 6, 4, 21)


def test_a_week_opening_on_the_sunday_before_a_monday_split_is_rebuilt_not_divided() -> None:
    """AMZN: the week stamped 4 June holds only prices after the split, though it opened before.

    Divided by twenty it would close at 6.26 and the next week would open at 120: the 19-fold gap
    the dry run of 14/10 found."""
    weeks = [
        week(SUNDAY_BEFORE, "2400", "2500", "2380", "2447"),
        week(SPLIT_WEEK, "125", "128", "121", "122"),
    ]
    days = [
        day(utc(2022, 6, 5, 21), "125", "128", "123", "124"),
        day(utc(2022, 6, 9, 21), "124", "126", "121", "122"),
    ]

    before, rebuilt = adjust(weeks, [AMZN], timeframe="W1", daily=days)

    assert before.close == Decimal("122.35")  # the week before ended before the split: ÷ 20
    assert (rebuilt.open, rebuilt.high, rebuilt.low, rebuilt.close) == (
        Decimal(125),
        Decimal(128),
        Decimal(121),
        Decimal(122),
    )
    assert rebuilt.tick_volume == 9999  # the week's own ticks
    assert rebuilt.real_volume == 20  # the days' shares
    assert rebuilt.spread == 1


def test_a_week_with_a_split_on_wednesday_takes_its_monday_from_the_adjusted_days() -> None:
    """SHOP's 10-for-1 on Wednesday 29 June 2022: the raw week opened at 330 and closed at 31.

    No factor fixes that bar; the adjusted days do."""
    shop = split(dt.date(2022, 6, 29), 10, 1)
    raw_week = week(utc(2022, 6, 25, 21), "330", "345", "29", "31")
    adjusted_days = [
        day(utc(2022, 6, 26, 21), "33.0", "34.5", "32.8", "34.0"),  # Monday, ÷ 10 already
        day(utc(2022, 6, 28, 21), "34.2", "34.4", "30.1", "31.0"),  # Wednesday, new shares
    ]

    [rebuilt] = adjust([raw_week], [shop], timeframe="W1", daily=adjusted_days)

    assert (rebuilt.open, rebuilt.high, rebuilt.low, rebuilt.close) == (
        Decimal("33.0"),
        Decimal("34.5"),
        Decimal("30.1"),
        Decimal("31.0"),
    )


def test_a_week_holding_a_split_without_its_days_is_refused() -> None:
    with pytest.raises(ValueError, match="collect D1 first"):
        adjust([week(SPLIT_WEEK, "125", "128", "121", "122")], [AMZN], timeframe="W1")


def test_a_daily_bar_holding_a_split_is_refused_since_nothing_finer_rebuilds_it() -> None:
    """A broker whose day does not start at its clock's midnight: refused, not guessed."""
    noon = utc(2022, 6, 5, 9)

    with pytest.raises(ValueError, match="straddles a split"):
        adjust([bar(noon, "2446.92")], [AMZN], timeframe="D1")


def test_the_gap_is_measured_from_the_last_week_that_ended_before_the_split() -> None:
    """From the last week that ended before it to the first that opened after it: the split
    week itself is on neither side — its close is already at the new price."""
    weeks = [
        week(SUNDAY_BEFORE, "2400", "2500", "2380", "2447"),
        week(SPLIT_WEEK, "125", "128", "121", "122"),
        week(utc(2022, 6, 11, 21), "121", "124", "117", "119"),
    ]

    observed = observed_gap(weeks, AMZN.effective_at, "W1")

    assert observed is not None
    assert math.isclose(observed, 121 / 2447)


def test_the_files_rebuild_the_split_week_from_the_stored_days(tmp_path: Path) -> None:
    write_candles(
        tmp_path,
        "AMZN",
        "D1",
        [
            day(utc(2022, 6, 2, 21), "2440", "2460", "2430", "2447"),
            day(utc(2022, 6, 5, 21), "125", "128", "123", "124"),
            day(utc(2022, 6, 9, 21), "124", "126", "121", "122"),
        ],
    )
    write_candles(
        tmp_path,
        "AMZN",
        "W1",
        [
            week(SUNDAY_BEFORE, "2400", "2500", "2380", "2447"),
            week(SPLIT_WEEK, "125", "128", "121", "122"),
        ],
    )

    bring_up_to_date(tmp_path, "AMZN", [AMZN])

    weeks = read_candles(tmp_path, "AMZN", "W1")
    assert [candle.close for candle in weeks] == [Decimal("122.35"), Decimal(122)]
    assert weeks[1].high == Decimal(128)


class _WeeksAndDays(_ListedSource):
    """A broker with raw weekly bars, and the raw daily bars they are made of."""

    def __init__(self, weeks: list[Candle], days: list[Candle]) -> None:
        super().__init__(weeks)
        self._days = days

    def candles(
        self, symbol: str, timeframe: str, start: dt.datetime, end: dt.datetime
    ) -> list[Candle]:
        bars = self._days if timeframe == "D1" else self._candles
        return [candle for candle in bars if start <= candle.time <= end]


def test_a_weekly_download_rebuilds_the_split_week_from_the_broker_s_days(tmp_path: Path) -> None:
    source = _WeeksAndDays(
        weeks=[
            week(SUNDAY_BEFORE, "2400", "2500", "2380", "2447"),
            week(SPLIT_WEEK, "125", "128", "121", "122"),
        ],
        days=[
            day(utc(2022, 6, 5, 21), "125", "128", "123", "124"),
            day(utc(2022, 6, 9, 21), "124", "126", "121", "122"),
            # The next week's Monday: a span that leaked past its end would take this low.
            day(utc(2022, 6, 12, 21), "119", "120", "100", "110"),
        ],
    )

    backfill(
        source,  # type: ignore[arg-type]
        root=tmp_path,
        symbol="EURUSD",
        timeframe="W1",
        start=utc(2022, 1, 1),
        end=utc(2022, 12, 31),
        splits=[AMZN],
    )

    weeks = read_candles(tmp_path, "EURUSD", "W1")
    assert [candle.close for candle in weeks] == [Decimal("122.35"), Decimal(122)]
    assert weeks[1].low == Decimal(121)


# --------------------------------------------------------------------------- #
# Finding the splits nobody recorded                                            #
# --------------------------------------------------------------------------- #


def daily(*open_close: tuple[str, str]) -> list[Candle]:
    """Consecutive daily bars, each `(open, close)`, stamped 21:00 UTC (a broker at UTC+3)."""
    start = utc(2022, 6, 1, 21)
    out = []
    for n, (opening, closing) in enumerate(open_close):
        body = [Decimal(opening), Decimal(closing)]
        out.append(
            Candle(
                time=start + dt.timedelta(days=n),
                open=body[0],
                high=max(body),
                low=min(body),
                close=body[1],
            )
        )
    return out


def test_a_20_for_1_gap_is_found_and_named_on_the_broker_s_day() -> None:
    [candidate] = find_candidates(daily(("2440", "2446.92"), ("125.20", "124")), THREE_HOURS)

    assert (candidate.shares_after, candidate.shares_before) == (20, 1)
    assert candidate.effective_on == dt.date(2022, 6, 3)  # 2 June 21:00 UTC is 3 June at UTC+3
    assert math.isclose(candidate.observed, 125.20 / 2446.92)


@pytest.mark.parametrize(
    ("opening", "why"),
    [
        ("70", "a -30 % earnings gap (NFLX, April 2022) is near no clean ratio"),
        ("142.7", "AMD's +42.7 % is 5 % from 3:2, outside the tolerance"),
        ("108", "an 8 % move is not looked at at all"),
    ],
)
def test_an_earnings_gap_is_not_a_split(opening: str, why: str) -> None:
    assert find_candidates(daily(("100", "100"), (opening, opening)), THREE_HOURS) == [], why


def test_a_3_for_2_is_found() -> None:
    [candidate] = find_candidates(daily(("90", "90"), ("60.2", "60")), THREE_HOURS)

    assert (candidate.shares_after, candidate.shares_before) == (3, 2)


# --------------------------------------------------------------------------- #
# The command line's two small parsers                                          #
# --------------------------------------------------------------------------- #


def test_the_ratio_is_after_colon_before() -> None:
    assert _ratio("20:1") == (20, 1)
    assert _ratio("1:10") == (1, 10)
    assert _day("2022-06-06") == dt.date(2022, 6, 6)
    for wrong in ("20", "20:0", "a:1", "-2:1"):
        with pytest.raises(argparse.ArgumentTypeError):
            _ratio(wrong)

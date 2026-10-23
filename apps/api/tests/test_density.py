"""Where a series' real bars begin — the rule, month by month (05/10), and the files it reads."""

import datetime as dt
import os
from decimal import Decimal
from pathlib import Path

from tradeforge_api.density import bars_by_month, real_from, real_start
from tradeforge_collector import write_candles
from tradeforge_engine.domain import Candle

# UsaTec M30 as measured on 05/10: one bar a day until 2016, then January, February and April 2017
# hold hourly bars stored as M30 — half a real month — and real M30 from May.
USATEC_M30 = {
    **{(2016, month): 21 for month in range(1, 13)},
    (2017, 1): 407,
    (2017, 2): 456,
    (2017, 3): 956,
    (2017, 4): 461,
    **{(2017, month): 970 for month in range(5, 13)},
    **{(year, month): 980 for year in (2018, 2019, 2020) for month in range(1, 13)},
    (2020, 12): 870,  # a December with its holidays is real
    (2021, 1): 400,  # the current month, not over
}
NOW = (2021, 1)


class TestTheRule:
    def test_the_real_bars_begin_after_the_last_thin_month(self) -> None:
        """March 2017 is full, but April is thin again: the real bars begin in May."""
        assert real_from(USATEC_M30, this_month=NOW) == (2017, 5)

    def test_a_series_real_from_its_first_month_is_left_alone(self) -> None:
        counts = {(2019, month): 1000 for month in range(1, 13)} | {
            (2020, month): 990 for month in range(1, 13)
        }
        assert real_from(counts, this_month=(2021, 1)) is None

    def test_the_current_month_is_never_judged(self) -> None:
        assert real_from({**USATEC_M30, NOW: 3}, this_month=NOW) == (2017, 5)

    def test_a_month_with_no_bar_inside_the_series_is_a_thin_one(self) -> None:
        """A whole month missing between two real ones is zero bars, not a month to skip."""
        counts = {month: 1000 for month in USATEC_M30 if month >= (2018, 1)}
        del counts[(2019, 6)]

        assert real_from(counts, this_month=NOW) == (2019, 7)

    def test_too_few_complete_months_say_nothing(self) -> None:
        assert real_from({(2020, 11): 20, (2020, 12): 1000}, this_month=NOW) is None

    def test_a_month_at_the_share_is_real_and_one_under_it_is_not(self) -> None:
        recent = {(year, month): 1000 for year in (2019, 2020) for month in range(1, 13)}

        assert real_from({(2018, 11): 590, (2018, 12): 600, **recent}, this_month=NOW) == (2018, 12)


def hourly(start: dt.datetime, hours: int) -> list[Candle]:
    level = Decimal("1.1")
    return [
        Candle(
            time=start + dt.timedelta(hours=hour),
            open=level,
            high=level,
            low=level,
            close=level,
            tick_volume=1,
        )
        for hour in range(hours)
    ]


def test_the_files_count_each_months_bars(tmp_path: Path) -> None:
    write_candles(tmp_path, "GOLD", "H1", hourly(dt.datetime(2020, 1, 31, 22, tzinfo=dt.UTC), 5))

    assert bars_by_month(tmp_path, "GOLD", "H1") == {(2020, 1): 2, (2020, 2): 3}
    assert bars_by_month(tmp_path, "GOLD", "M15") == {}


def test_a_series_never_collected_has_no_start(tmp_path: Path) -> None:
    assert real_start(tmp_path, "GOLD", "H1", today=dt.date(2026, 10, 2)) is None


def test_the_start_is_kept_until_the_files_change(tmp_path: Path) -> None:
    """Asked by every run for its warm-up: read once, read again only after a re-collection."""
    start = dt.datetime(2019, 1, 1, tzinfo=dt.UTC)
    thin = [bar for bar in hourly(start, 24 * 31) if bar.time.hour == 0]
    real = hourly(dt.datetime(2019, 2, 1, tzinfo=dt.UTC), 24 * 486)  # to the end of May 2020
    write_candles(tmp_path, "GOLD", "H1", [*thin, *real])
    today = dt.date(2020, 6, 15)

    assert real_start(tmp_path, "GOLD", "H1", today=today) == dt.datetime(2019, 2, 1, tzinfo=dt.UTC)

    # The collector fills January with real bars: the files change, and so does the answer.
    write_candles(tmp_path, "GOLD", "H1", hourly(start, 24 * 31))
    for path in (tmp_path / "symbol=GOLD" / "timeframe=H1").rglob("*.parquet"):
        os.utime(path, ns=(path.stat().st_atime_ns, path.stat().st_mtime_ns + 1_000_000))
    assert real_start(tmp_path, "GOLD", "H1", today=today) is None

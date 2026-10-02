"""Where a series' real bars begin — the rule, and the footers it reads (02/10)."""

import datetime as dt
from decimal import Decimal
from pathlib import Path

from tradeforge_api.density import bars_by_year, real_from, real_start
from tradeforge_collector import write_candles
from tradeforge_engine.domain import Candle

# UsaInd H4 as measured on 02/10: one bar a day until 2016, then real H4.
USAIND_H4 = {
    2013: 191,
    2014: 262,
    2015: 263,
    2016: 259,
    2017: 1527,
    2018: 1540,
    2019: 1543,
    2020: 1549,
    2021: 1544,
    2022: 1543,
    2023: 1540,
    2024: 1548,
    2025: 1538,
    2026: 1169,
}


class TestTheRule:
    def test_the_real_bars_begin_where_every_later_year_is_full(self) -> None:
        assert real_from(USAIND_H4, this_year=2026) == 2017

    def test_a_series_real_from_its_first_complete_year_is_left_alone(self) -> None:
        assert real_from({2020: 6200, 2021: 6210, 2022: 6190, 2023: 6205}, this_year=2026) is None

    def test_the_current_year_is_never_judged(self) -> None:
        """2026 is not over: its 1 169 bars are not a sparse year."""
        assert real_from({**USAIND_H4, 2026: 10}, this_year=2026) == 2017

    def test_a_sparse_year_after_real_ones_moves_the_start_past_it(self) -> None:
        counts = {2018: 6000, 2019: 6000, 2020: 300, 2021: 6000, 2022: 6000, 2023: 6000}

        assert real_from(counts, this_year=2026) == 2021

    def test_too_few_complete_years_say_nothing(self) -> None:
        assert real_from({2024: 260, 2025: 6000, 2026: 3000}, this_year=2026) is None

    def test_a_year_at_the_share_is_real_and_one_under_it_is_not(self) -> None:
        recent = {2021: 1000, 2022: 1000, 2023: 1000, 2024: 1000, 2025: 1000}

        assert real_from({2019: 790, 2020: 800, **recent}, this_year=2026) == 2020


def daily(start: dt.datetime, days: int) -> list[Candle]:
    level = Decimal("1.1")
    return [
        Candle(
            time=start + dt.timedelta(days=day),
            open=level,
            high=level,
            low=level,
            close=level,
            tick_volume=1,
        )
        for day in range(days)
    ]


def test_the_footers_count_each_years_bars(tmp_path: Path) -> None:
    write_candles(tmp_path, "GOLD", "H1", daily(dt.datetime(2020, 1, 1, tzinfo=dt.UTC), 400))

    assert bars_by_year(tmp_path, "GOLD", "H1") == {2020: 366, 2021: 34}
    assert bars_by_year(tmp_path, "GOLD", "M15") == {}


def test_a_series_never_collected_has_no_start(tmp_path: Path) -> None:
    assert real_start(tmp_path, "GOLD", "H1", today=dt.date(2026, 10, 2)) is None

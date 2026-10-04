"""The worker's candle cache: the same bars as a fresh read, and never bars the disk no longer has.

No database here — the cache sits between a run and the Parquet files, so every test writes real
files under `tmp_path` and reads them back through the real `read_candles`, counting how often it
is called.
"""

import datetime as dt
import os
from collections.abc import Sequence
from decimal import Decimal
from pathlib import Path

import pytest

from tradeforge_api.candle_cache import CandleCache, fingerprint, read_window
from tradeforge_api.warm_window import WarmUp, warm_start
from tradeforge_collector import read_candles, write_candles
from tradeforge_collector.storage import dataset_path
from tradeforge_engine.domain import Candle

SYMBOL = "GBPUSD"
TF = "M15"


def candles(start: dt.datetime, count: int, *, price: str = "1.25000") -> list[Candle]:
    level = Decimal(price)
    return [
        Candle(
            time=start + i * dt.timedelta(minutes=15),
            open=level,
            high=level,
            low=level,
            close=level,
            tick_volume=1,
        )
        for i in range(count)
    ]


START = dt.datetime(2024, 3, 1, tzinfo=dt.UTC)


class Counting:
    """`read_candles`, counting its calls."""

    def __init__(self) -> None:
        self.calls = 0
        self.asked: list[tuple[dt.datetime | None, dt.datetime | None]] = []

    def __call__(
        self,
        root: Path,
        symbol: str,
        timeframe: str,
        *,
        start: dt.datetime | None = None,
        end: dt.datetime | None = None,
    ) -> Sequence[Candle]:
        self.calls += 1
        self.asked.append((start, end))
        return read_candles(root, symbol, timeframe, start=start, end=end)


class TestAHit:
    def test_reads_the_files_once_and_answers_what_they_hold(self, tmp_path: Path) -> None:
        write_candles(tmp_path, SYMBOL, TF, candles(START, 20))
        reader = Counting()
        cache = CandleCache(reader=reader)

        first = cache.read(tmp_path, SYMBOL, TF)
        second = cache.read(tmp_path, SYMBOL, TF)

        assert reader.calls == 1
        assert list(first) == read_candles(tmp_path, SYMBOL, TF)
        assert second is first

    def test_hands_out_a_tuple_so_no_run_can_alter_the_next_runs_bars(self, tmp_path: Path) -> None:
        write_candles(tmp_path, SYMBOL, TF, candles(START, 5))

        assert isinstance(CandleCache().read(tmp_path, SYMBOL, TF), tuple)

    def test_keeps_each_symbol_and_timeframe_apart(self, tmp_path: Path) -> None:
        write_candles(tmp_path, SYMBOL, TF, candles(START, 5))
        write_candles(tmp_path, "EURUSD", TF, candles(START, 7))
        write_candles(tmp_path, SYMBOL, "H1", candles(START, 9))
        cache = CandleCache(capacity=3)

        assert len(cache.read(tmp_path, SYMBOL, TF)) == 5
        assert len(cache.read(tmp_path, "EURUSD", TF)) == 7
        assert len(cache.read(tmp_path, SYMBOL, "H1")) == 9


class TestTheFilesChanging:
    """⚠️ The reason the key is a fingerprint: a re-collection must reach the very next run."""

    def test_a_rewritten_year_is_read_again(self, tmp_path: Path) -> None:
        write_candles(tmp_path, SYMBOL, TF, candles(START, 20))
        reader = Counting()
        cache = CandleCache(reader=reader)
        cache.read(tmp_path, SYMBOL, TF)

        write_candles(tmp_path, SYMBOL, TF, candles(START, 30, price="1.26000"))
        after = cache.read(tmp_path, SYMBOL, TF)

        assert reader.calls == 2
        assert len(after) == 30
        assert after[0].close == Decimal("1.26000")

    def test_a_new_year_is_read_again(self, tmp_path: Path) -> None:
        write_candles(tmp_path, SYMBOL, TF, candles(START, 20))
        cache = CandleCache()
        cache.read(tmp_path, SYMBOL, TF)

        # `write_candles` only replaces the years it writes: 2024 stays, 2025 joins it.
        write_candles(tmp_path, SYMBOL, TF, candles(dt.datetime(2025, 1, 2, tzinfo=dt.UTC), 4))

        assert len(cache.read(tmp_path, SYMBOL, TF)) == 24

    def test_a_file_touched_without_changing_size_is_read_again(self, tmp_path: Path) -> None:
        write_candles(tmp_path, SYMBOL, TF, candles(START, 20))
        reader = Counting()
        cache = CandleCache(reader=reader)
        cache.read(tmp_path, SYMBOL, TF)

        # Same bytes count, later modification time: what a same-length rewrite looks like to
        # the fingerprint when two versions happen to compress to the same size.
        (part,) = Path(dataset_path(tmp_path, SYMBOL, TF)).rglob("*.parquet")
        stat = part.stat()
        os.utime(part, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
        cache.read(tmp_path, SYMBOL, TF)

        assert reader.calls == 2

    def test_a_symbol_collected_after_an_empty_read_is_seen(self, tmp_path: Path) -> None:
        cache = CandleCache()
        assert cache.read(tmp_path, SYMBOL, TF) == ()

        write_candles(tmp_path, SYMBOL, TF, candles(START, 3))

        assert len(cache.read(tmp_path, SYMBOL, TF)) == 3

    def test_a_read_that_raced_a_rewrite_is_answered_but_never_kept(self, tmp_path: Path) -> None:
        write_candles(tmp_path, SYMBOL, TF, candles(START, 20))
        calls = 0

        def racing(
            root: Path,
            symbol: str,
            timeframe: str,
            *,
            start: dt.datetime | None = None,
            end: dt.datetime | None = None,
        ) -> Sequence[Candle]:
            nonlocal calls
            calls += 1
            read = read_candles(root, symbol, timeframe)
            if calls == 1:  # the collector rewrites the year while the run is reading it
                write_candles(root, symbol, timeframe, candles(START, 25))
            return read

        cache = CandleCache(reader=racing)

        assert len(cache.read(tmp_path, SYMBOL, TF)) == 20
        assert len(cache.read(tmp_path, SYMBOL, TF)) == 25
        assert calls == 2


class TestTheBound:
    def test_the_least_recently_read_dataset_goes_first(self, tmp_path: Path) -> None:
        for symbol in ("AAA", "BBB", "CCC"):
            write_candles(tmp_path, symbol, TF, candles(START, 3))
        reader = Counting()
        cache = CandleCache(capacity=2, reader=reader)

        cache.read(tmp_path, "AAA", TF)
        cache.read(tmp_path, "BBB", TF)
        cache.read(tmp_path, "AAA", TF)  # AAA is now the most recent; BBB the oldest
        cache.read(tmp_path, "CCC", TF)  # evicts BBB
        assert reader.calls == 3

        cache.read(tmp_path, "AAA", TF)
        assert reader.calls == 3
        cache.read(tmp_path, "BBB", TF)
        assert reader.calls == 4

    def test_refuses_to_hold_nothing(self) -> None:
        with pytest.raises(ValueError, match="at least one"):
            CandleCache(capacity=0)


class TestTheFingerprint:
    def test_is_empty_for_a_dataset_never_collected(self, tmp_path: Path) -> None:
        assert fingerprint(tmp_path, SYMBOL, TF) == ()

    def test_names_every_year_file_in_a_fixed_order(self, tmp_path: Path) -> None:
        write_candles(tmp_path, SYMBOL, TF, candles(dt.datetime(2025, 1, 2, tzinfo=dt.UTC), 3))
        write_candles(tmp_path, SYMBOL, TF, candles(START, 3))

        names = [name for name, _, _ in fingerprint(tmp_path, SYMBOL, TF)]

        assert names == ["year=2024/part-0.parquet", "year=2025/part-0.parquet"]


def hourly(start: dt.datetime, end: dt.datetime) -> list[Candle]:
    """An H1 bar every hour of every weekday from `start` to `end` — a market shut on weekends."""
    bars: list[Candle] = []
    at = start
    level = Decimal("1.25000")
    while at <= end:
        if at.weekday() < 5:
            bars.append(
                Candle(time=at, open=level, high=level, low=level, close=level, tick_volume=1)
            )
        at += dt.timedelta(hours=1)
    return bars


def warmed(candles: Sequence[Candle], date_from: dt.datetime, warmup: WarmUp) -> list[Candle]:
    """The bars a run over `date_from` onwards reads: its warm-up and its window."""
    return list(candles[warm_start(candles, warmup, date_from) :])


class TestTheWindow:
    """02/10: twelve workers each holding whole M5 series since 1993 filled 31 GB."""

    FROM = dt.datetime(2022, 3, 1, tzinfo=dt.UTC)
    TO = dt.datetime(2022, 6, 30, 23, tzinfo=dt.UTC)

    @pytest.mark.parametrize(
        "warmup",
        [
            WarmUp(bars=0, span=None),
            WarmUp(bars=800, span=None),
            WarmUp(bars=80, span=dt.timedelta(days=365)),
        ],
    )
    def test_a_run_reads_the_bars_a_whole_read_would_hand_it(
        self, tmp_path: Path, warmup: WarmUp
    ) -> None:
        write_candles(
            tmp_path, SYMBOL, "H1", hourly(dt.datetime(2015, 1, 1, tzinfo=dt.UTC), self.TO)
        )
        whole = read_candles(tmp_path, SYMBOL, "H1", end=self.TO)

        read = read_window(
            CandleCache().read,
            tmp_path,
            SYMBOL,
            "H1",
            date_from=self.FROM,
            date_to=self.TO,
            warmup=warmup,
        )

        assert warmed(read, self.FROM, warmup) == warmed(whole, self.FROM, warmup)
        assert read[0].time.year >= 2020, "it read years the run never needs"
        assert read[-1].time <= self.TO

    def test_a_series_that_starts_inside_the_warm_up_is_read_whole(self, tmp_path: Path) -> None:
        """Too few bars before the window: the read falls back to every bar there is."""
        write_candles(
            tmp_path, SYMBOL, "H1", hourly(dt.datetime(2022, 2, 25, tzinfo=dt.UTC), self.TO)
        )
        reader = Counting()
        warmup = WarmUp(bars=800, span=None)

        read = read_window(
            CandleCache(reader=reader).read,
            tmp_path,
            SYMBOL,
            "H1",
            date_from=self.FROM,
            date_to=self.TO,
            warmup=warmup,
        )

        assert reader.calls == 2
        assert reader.asked[-1][0] is None
        assert warmed(read, self.FROM, warmup) == warmed(
            read_candles(tmp_path, SYMBOL, "H1", end=self.TO), self.FROM, warmup
        )

    def test_the_variants_of_one_sweep_share_one_read(self, tmp_path: Path) -> None:
        """Warm-ups that differ by a few hundred bars start on the same 1 January."""
        write_candles(
            tmp_path, SYMBOL, "H1", hourly(dt.datetime(2019, 1, 1, tzinfo=dt.UTC), self.TO)
        )
        reader = Counting()
        cache = CandleCache(reader=reader)

        for bars in (0, 80, 200, 400):
            read_window(
                cache.read,
                tmp_path,
                SYMBOL,
                "H1",
                date_from=self.FROM,
                date_to=self.TO,
                warmup=WarmUp(bars=bars, span=None),
            )

        assert reader.calls == 1


def daily_as_hourly(start: dt.datetime, end: dt.datetime) -> list[Candle]:
    """One bar a weekday stored as H1 — the broker's old intraday history (`density`)."""
    return [bar for bar in hourly(start, end) if bar.time.hour == 0]


class TestTheWarmUpStopsAtTheRealBars:
    """04/10: UsaTec M15 from 2018 warmed on the thin bars of early 2017 and never traded."""

    REAL = dt.datetime(2017, 1, 1, tzinfo=dt.UTC)
    TO = dt.datetime(2022, 12, 31, 23, tzinfo=dt.UTC)
    WARMUP = WarmUp(bars=80, span=dt.timedelta(days=365))

    def write(self, root: Path) -> None:
        old = daily_as_hourly(dt.datetime(2014, 1, 1, tzinfo=dt.UTC), self.REAL)
        real = hourly(self.REAL, self.TO)
        write_candles(
            root,
            SYMBOL,
            "H1",
            [*old[:-1], *real] if old[-1].time == real[0].time else [*old, *real],
        )

    def read(self, root: Path, date_from: dt.datetime) -> Sequence[Candle]:
        return read_window(
            CandleCache().read,
            root,
            SYMBOL,
            "H1",
            date_from=date_from,
            date_to=self.TO,
            warmup=self.WARMUP,
        )

    def test_a_window_just_after_the_real_start_warms_on_real_bars_only(
        self, tmp_path: Path
    ) -> None:
        self.write(tmp_path)
        date_from = dt.datetime(2017, 3, 1, tzinfo=dt.UTC)

        read = self.read(tmp_path, date_from)

        assert read[0].time >= self.REAL, "the warm-up read the daily bars before the real ones"
        assert [bar.time for bar in read if bar.time >= date_from] == [
            bar.time for bar in hourly(date_from, self.TO)
        ]

    def test_a_window_inside_the_daily_bars_keeps_its_own_bars(self, tmp_path: Path) -> None:
        """The cut never reaches into the window: a run asked over the old years reads them."""
        self.write(tmp_path)
        date_from = dt.datetime(2016, 6, 1, tzinfo=dt.UTC)

        read = self.read(tmp_path, date_from)

        assert read[0].time >= date_from
        assert read[0].time < self.REAL

    def test_a_window_far_from_the_real_start_is_read_as_before(self, tmp_path: Path) -> None:
        self.write(tmp_path)
        date_from = dt.datetime(2020, 3, 1, tzinfo=dt.UTC)
        whole = read_candles(tmp_path, SYMBOL, "H1", end=self.TO)

        read = self.read(tmp_path, date_from)

        assert warmed(read, date_from, self.WARMUP) == warmed(whole, date_from, self.WARMUP)

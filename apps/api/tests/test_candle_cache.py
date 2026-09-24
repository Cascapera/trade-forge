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

from tradeforge_api.candle_cache import CandleCache, fingerprint
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

    def __call__(self, root: Path, symbol: str, timeframe: str) -> Sequence[Candle]:
        self.calls += 1
        return read_candles(root, symbol, timeframe)


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

        def racing(root: Path, symbol: str, timeframe: str) -> Sequence[Candle]:
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

"""The worker's memory of the candles it has just read.

A sweep is a million runs of one symbol at one timeframe, and every one of them used to open the
same Parquet files and build the same hundred thousand `Candle` objects before throwing them
away. Measured on 24/09, on the worker, over GBPUSD M15: the read was 0.65 s of a 1.6 s run —
four tenths of a sweep's time spent re-reading what had not changed.

So the worker keeps what it read, and reads again only when the files underneath have changed.

⚠️ **The key is what is on disk, not the name.** A collector that re-collects GBPUSD M15 rewrites
its year files (`storage.write_candles`, `delete_matching`); a cache keyed on (symbol, timeframe)
alone would go on serving the old bars to every run after it, and those runs would record the
old window as what they read. Each entry carries the *fingerprint* of the dataset — every
`.parquet` file's path, size and modification time — and a lookup whose fingerprint differs is a
miss. The fingerprint is taken before and after the read: a read that raced a rewrite is used
once and never kept, because it may hold half of each version.

Per process, never shared. Candles are frozen, and the cache hands out a tuple, so no run can
alter the bars the next run reads. Bounded, because each entry is ~60 MB for a decade of M15 and
every worker holds its own: the default keeps four.

⚠️ **A run reads its window, not the whole series** (`read_window`, 02/10). The first sweep over
the ActivTrades data put twelve workers at 31 GB of 31: each held whole M5 series since 1993 — 3
to 5 million bars — for runs over five years, and runs failed with "Cannot allocate memory". So
the entry is keyed on the bounds read as well, and a run asks for its window and its warm-up.
"""

from __future__ import annotations

import datetime as dt
from bisect import bisect_left
from collections import OrderedDict
from collections.abc import Sequence
from pathlib import Path
from typing import Protocol

from tradeforge_api.density import real_start
from tradeforge_api.warm_window import WarmUp
from tradeforge_collector import Candle, read_candles
from tradeforge_collector.storage import dataset_path
from tradeforge_collector.timeframes import step


class CandleReader(Protocol):
    """What a run calls to get its bars: `(parquet_root, symbol, timeframe)`, bounded by `start`
    and `end` (inclusive, as `read_candles`) or the whole series when they are left out."""

    def __call__(
        self,
        root: Path,
        symbol: str,
        timeframe: str,
        *,
        start: dt.datetime | None = None,
        end: dt.datetime | None = None,
    ) -> Sequence[Candle]: ...


GAP_FACTOR = 5
"""Calendar time per bar of warm-up, over the chart's own step: a market shut at night and on
weekends has fewer bars than the clock (an index's session, a share's 6.5 hours). Too little only
costs a second read (`read_window`); it never changes what a run reads."""

Fingerprint = tuple[tuple[str, int, int], ...]


def fingerprint(root: Path, symbol: str, timeframe: str) -> Fingerprint:
    """Every Parquet file of the dataset, with its size and modification time, in a fixed order.

    A missing directory is the empty fingerprint, the same as a directory with no files — which
    is also what `read_candles` answers for both: no bars.
    """
    directory = Path(dataset_path(root, symbol, timeframe))
    if not directory.exists():
        return ()
    files = []
    for path in directory.rglob("*.parquet"):
        stat = path.stat()
        files.append((path.relative_to(directory).as_posix(), stat.st_size, stat.st_mtime_ns))
    return tuple(sorted(files))


class CandleCache:
    """`read` has `read_candles`' answer, remembered while the files it came from stay the same."""

    def __init__(self, *, capacity: int = 4, reader: CandleReader = read_candles) -> None:
        if capacity < 1:
            raise ValueError(f"a candle cache holds at least one dataset, not {capacity}")
        self._capacity = capacity
        self._reader = reader
        self._entries: OrderedDict[_Key, tuple[Fingerprint, tuple[Candle, ...]]] = OrderedDict()

    def read(
        self,
        root: Path,
        symbol: str,
        timeframe: str,
        *,
        start: dt.datetime | None = None,
        end: dt.datetime | None = None,
    ) -> Sequence[Candle]:
        key = (root, symbol, timeframe, start, end)
        before = fingerprint(root, symbol, timeframe)
        entry = self._entries.get(key)
        if entry is not None and entry[0] == before:
            self._entries.move_to_end(key)
            return entry[1]

        candles = tuple(self._reader(root, symbol, timeframe, start=start, end=end))
        # Kept only if nothing was rewritten while it was read (see the module docstring). A stale
        # entry is dropped either way: it can never be the answer again.
        self._entries.pop(key, None)
        if fingerprint(root, symbol, timeframe) == before:
            self._entries[key] = (before, candles)
            while len(self._entries) > self._capacity:
                self._entries.popitem(last=False)
        return candles


def read_window(  # noqa: PLR0913 — keyword-only; each bounds the read
    read: CandleReader,
    root: Path,
    symbol: str,
    timeframe: str,
    *,
    date_from: dt.datetime,
    date_to: dt.datetime,
    warmup: WarmUp,
) -> Sequence[Candle]:
    """The bars a run over `[date_from, date_to]` reads: its window and its warm-up, never the
    whole series — and exactly the bars a read of the whole series would have handed it.

    ⚠️ **The same bars, or the whole series.** A run warms on `warmup.bars` before its window
    (`warm_window.warm_start`), and the reuse of a run compares how many it read (`reuse`). The read
    starts early enough for that on any market that trades (`GAP_FACTOR`), on a 1 January whole
    years before the window's, so the variants of one sweep share an entry. When it still finds
    fewer bars before the window than the warm-up asks — the series starts there, or the market
    barely trades — it reads the whole series, which is what every run read before.

    ⚠️ **The warm-up stops where the real bars begin (04/10).** Before some year a broker's
    intraday history is thin or one bar a day stored as the chart (`density`); a launch starts the
    window at the first real year (#379), but the warm-up still reached back into the thin bars.
    Measured on UsaTec M15 over 2018-2024: warmed on 2017, whose January and February hold about
    400 bars a month against 1 900, a run made no trade in seven years; warmed from April 2017, or
    not at all, the same run made 157. So the warm-up's bars before the series' first real year are
    dropped: it warms on less, as it does where a series simply starts. The window's own bars are
    never dropped.
    """
    candles = _read_window(
        read, root, symbol, timeframe, date_from=date_from, date_to=date_to, warmup=warmup
    )
    real = real_start(root, symbol, timeframe)
    if real is None:
        return candles
    cut = min(real, date_from)
    first = bisect_left(candles, cut, key=lambda candle: candle.time)
    return candles if first == 0 else candles[first:]


def _read_window(  # noqa: PLR0913 — keyword-only; each bounds the read
    read: CandleReader,
    root: Path,
    symbol: str,
    timeframe: str,
    *,
    date_from: dt.datetime,
    date_to: dt.datetime,
    warmup: WarmUp,
) -> Sequence[Candle]:
    """`read_window` before the cut at the real bars: the window and the warm-up asked for."""
    lead = warmup.bars * step(timeframe) * GAP_FACTOR
    if warmup.span is not None:
        lead = max(lead, warmup.span)
    # Whole years back from the window's own: every warm-up of up to a year starts on the same
    # 1 January, so the variants of one sweep share one entry of the cache.
    years = -(-(lead + dt.timedelta(days=7)) // dt.timedelta(days=365))
    start = dt.datetime(date_from.year - years, 1, 1, tzinfo=dt.UTC)
    candles = read(root, symbol, timeframe, start=start, end=date_to)
    before = bisect_left(candles, date_from, key=lambda candle: candle.time)
    if before >= warmup.bars:
        return candles
    return read(root, symbol, timeframe, end=date_to)


_Key = tuple[Path, str, str, dt.datetime | None, dt.datetime | None]

__all__ = ["CandleCache", "CandleReader", "fingerprint", "read_window"]

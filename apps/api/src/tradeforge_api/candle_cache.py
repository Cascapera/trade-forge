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
every worker holds its own: the default keeps two, the symbol a sweep is on and the one before.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable, Sequence
from pathlib import Path

from tradeforge_collector import Candle, read_candles
from tradeforge_collector.storage import dataset_path

CandleReader = Callable[[Path, str, str], Sequence[Candle]]
"""What a run calls to get its bars: `(parquet_root, symbol, timeframe) -> candles`."""

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

    def __init__(self, *, capacity: int = 2, reader: CandleReader = read_candles) -> None:
        if capacity < 1:
            raise ValueError(f"a candle cache holds at least one dataset, not {capacity}")
        self._capacity = capacity
        self._reader = reader
        self._entries: OrderedDict[tuple[Path, str, str], tuple[Fingerprint, tuple[Candle, ...]]]
        self._entries = OrderedDict()

    def read(self, root: Path, symbol: str, timeframe: str) -> Sequence[Candle]:
        key = (root, symbol, timeframe)
        before = fingerprint(root, symbol, timeframe)
        entry = self._entries.get(key)
        if entry is not None and entry[0] == before:
            self._entries.move_to_end(key)
            return entry[1]

        candles = tuple(self._reader(root, symbol, timeframe))
        # Kept only if nothing was rewritten while it was read (see the module docstring). A stale
        # entry is dropped either way: it can never be the answer again.
        self._entries.pop(key, None)
        if fingerprint(root, symbol, timeframe) == before:
            self._entries[key] = (before, candles)
            while len(self._entries) > self._capacity:
                self._entries.popitem(last=False)
        return candles


__all__ = ["CandleCache", "CandleReader", "fingerprint"]

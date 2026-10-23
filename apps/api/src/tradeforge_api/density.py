"""Where a series' real bars begin — read from how many bars each month holds (02/10, by month
since 05/10).

The data-quality scan of the ActivTrades collection found that, before some date, a broker's
intraday history is not intraday at all: UsaInd H4 holds about 260 bars a year until 2016 — one a
day, stored as H4 — and 1 540 from 2017. A run over those years trades a setup meant for H4 on
daily bars, and its result is not a result.

⚠️ **By month, not by year (05/10).** The first rule judged whole years, and UsaTec M30 passed
2017 as real — yet its January, February and April hold 407, 456 and 461 bars against about 970
in a real month. Those months, read inside a window or its warm-up, left every structure setup of
the sweep with no trade in seven years (the same run from 2019 made 278). A year that is mostly
real still hides its thin months; a month does not.

**The rule.** A month is real when it holds at least `REAL_SHARE` of the bars the series' recent
complete months hold (their median over `RECENT_MONTHS`); the real bars begin at the first month
from which every later complete month is real. A month with no bar at all inside the series
counts as zero. The current month is never judged — it is not over.

⚠️ **Why 0.6.** Measured on 05/10: a real month holds 85 to 100% of the median — February is short
and December has holidays — and the thin months 42 to 48%. Sixty per cent leaves room on both
sides; the year rule's eighty would have flagged a real February.

Read from the `time` column only, and kept per series until its files change (the same fingerprint
the candle cache keys on): a launch asks it of every market and chart, and every run asks it again
for its warm-up (`candle_cache.read_window`).
"""

import datetime as dt
import statistics
from collections import Counter
from pathlib import Path

import pyarrow.compute as pc
import pyarrow.parquet as pq

from tradeforge_collector.storage import dataset_path

REAL_SHARE = 0.6
"""The share of a recent month's bars a month must hold to be real (see the module docstring)."""

RECENT_MONTHS = 24
"""How many of the latest complete months set what a real month holds."""

MIN_COMPLETE_MONTHS = 12
"""Fewer complete months than this, and there is nothing to judge a month against."""

Month = tuple[int, int]

_KEPT: dict[tuple[str, str, str], tuple[object, dt.datetime | None]] = {}


def bars_by_month(root: Path, symbol: str, timeframe: str) -> dict[Month, int]:
    """How many bars each month of the series holds, from the `time` column of its files."""
    directory = Path(dataset_path(root, symbol, timeframe))
    counts: Counter[Month] = Counter()
    if not directory.exists():
        return {}
    for path in directory.glob("year=*/*.parquet"):
        times = pq.read_table(path, columns=["time"]).column("time")
        years = pc.year(times).to_pylist()
        months = pc.month(times).to_pylist()
        counts.update(zip(years, months, strict=True))
    return dict(counts)


def _months_between(first: Month, last: Month) -> list[Month]:
    out = []
    year, month = first
    while (year, month) <= last:
        out.append((year, month))
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)  # noqa: PLR2004 — December
    return out


def real_from(counts: dict[Month, int], *, this_month: Month) -> Month | None:
    """The first month of real bars, or `None` when every month is real or there is too little to
    tell — in both cases a window is left as it was asked."""
    held = [month for month in counts if month < this_month]
    if not held:
        return None
    complete = _months_between(min(held), max(held))
    if len(complete) < MIN_COMPLETE_MONTHS:
        return None
    recent = [counts.get(month, 0) for month in complete[-RECENT_MONTHS:]]
    floor = REAL_SHARE * statistics.median(recent)
    start: Month | None = None
    for month in complete:
        if counts.get(month, 0) >= floor:
            if start is None:
                start = month
        else:
            start = None
    if start is None or start == complete[0]:
        return None
    return start


def real_start(
    root: Path, symbol: str, timeframe: str, *, today: dt.date | None = None
) -> dt.datetime | None:
    """The instant the series' real bars begin (the 1st of `real_from`'s month), or `None`."""
    # Imported here: the candle cache reads this module, and the fingerprint is the cache's.
    from tradeforge_api.candle_cache import fingerprint  # noqa: PLC0415

    now = today or dt.datetime.now(tz=dt.UTC).date()
    key = (root.as_posix(), symbol, timeframe)
    stamp = (fingerprint(root, symbol, timeframe), now.year, now.month)
    kept = _KEPT.get(key)
    if kept is not None and kept[0] == stamp:
        return kept[1]
    month = real_from(bars_by_month(root, symbol, timeframe), this_month=(now.year, now.month))
    start = None if month is None else dt.datetime(month[0], month[1], 1, tzinfo=dt.UTC)
    _KEPT[key] = (stamp, start)
    return start


__all__ = ["REAL_SHARE", "bars_by_month", "real_from", "real_start"]

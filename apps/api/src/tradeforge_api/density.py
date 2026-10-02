"""Where a series' real bars begin — read from how many bars each year holds (02/10).

The data-quality scan of the ActivTrades collection found that, before some year, a broker's
intraday history is not intraday at all: UsaInd H4 holds about 260 bars a year until 2016 — one a
day, stored as H4 — and 1 540 from 2017. A run over those years trades a setup meant for H4 on
daily bars, and its result is not a result. The first year of real bars, per chart:

* the metals' and US indices' M15 and M30 from 2017-2018, their H1 and H4 from 2015-2018;
* XRP from 2018, ETH from 2016;
* the currency pairs from 1999-2010, so nothing a window from 2010 onwards reaches.

**The rule.** A year is real when it holds at least `REAL_SHARE` of the bars the series' recent
complete years hold (their median); the real bars begin at the first year from which every later
complete year is real. The current year is never judged — it is not over.

Read from the Parquet footers alone (`num_rows`): no bar is decoded, so a launch can ask it of
every market and chart it is about to run.
"""

import datetime as dt
import statistics
from pathlib import Path

import pyarrow.parquet as pq

from tradeforge_collector.storage import dataset_path

REAL_SHARE = 0.8
"""The share of a recent year's bars a year must hold to be real — measured on 02/10: the
synthetic years hold a fifth to a twentieth of a real one's, the real ones within a few percent."""

RECENT_YEARS = 5
"""How many of the latest complete years set what a real year holds."""

MIN_COMPLETE_YEARS = 3
"""Fewer complete years than this, and there is nothing to judge a year against."""


def bars_by_year(root: Path, symbol: str, timeframe: str) -> dict[int, int]:
    """How many bars each year of the series holds, from its files' footers."""
    directory = Path(dataset_path(root, symbol, timeframe))
    counts: dict[int, int] = {}
    if not directory.exists():
        return counts
    for path in directory.glob("year=*/*.parquet"):
        year = int(path.parent.name.split("=", 1)[1])
        counts[year] = counts.get(year, 0) + pq.ParquetFile(path).metadata.num_rows
    return counts


def real_from(counts: dict[int, int], *, this_year: int) -> int | None:
    """The first year of real bars, or `None` when every year is real or there is too little to
    tell — in both cases a window is left as it was asked."""
    complete = sorted(year for year in counts if year < this_year)
    if len(complete) < MIN_COMPLETE_YEARS:
        return None
    recent = [counts[year] for year in complete[-RECENT_YEARS:]]
    floor = REAL_SHARE * statistics.median(recent)
    start: int | None = None
    for year in complete:
        if counts[year] >= floor:
            if start is None:
                start = year
        else:
            start = None
    if start is None or start == complete[0]:
        return None
    return start


def real_start(
    root: Path, symbol: str, timeframe: str, *, today: dt.date | None = None
) -> dt.datetime | None:
    """The instant the series' real bars begin (1 January of `real_from`), or `None`."""
    year = real_from(
        bars_by_year(root, symbol, timeframe),
        this_year=(today or dt.datetime.now(tz=dt.UTC).date()).year,
    )
    return None if year is None else dt.datetime(year, 1, 1, tzinfo=dt.UTC)


__all__ = ["REAL_SHARE", "bars_by_year", "real_from", "real_start"]

"""What a window still needs collected before a run over it can read every bar.

A pure function over plain facts — what the `datasets` index says is on disk, the oldest bar the
broker will hand over, and the clock — so the rules are tested by hand-worked dates rather than
through a database. The router reads the facts; nothing here knows where they came from.

⚠️ **Whole calendar years, because anything less destroys data.** The collector writes one Parquet
partition per year and replaces the partitions it touches (`write_candles`,
`existing_data_behavior="delete_matching"`). Measured on 17/09/2026: a year of twelve bars, then a
write of one bar dated 2024-12-20, left one bar. Collecting "only the missing fortnight" of a year
already on disk would erase the other fifty weeks.

⚠️ **A window reaches the data already on disk, even past what was asked.** The index keeps one
extent per (symbol, timeframe) — the first and the last bar — so it cannot represent a hole. Asked
for 2015-2016 over data from 2020, collecting only 2015-2016 would make the index read "2015 to
2026" with 2017-2019 missing, and the next request for 2018 would be told it is covered.

⚠️ **The index is the authority on what exists, not the log of past collections.** Measured on
this project: `collections` holds finished requests for EURGBP, AUDCAD, XAUUSD and BTCUSD whose
data is no longer in `datasets`. A request that finished once proves only that it finished once.

⚠️ **Not a hole detector.** A partition erased as above, under an extent that still spans it, is
invisible here: the extent is all the index knows, and reading the files is exactly what the
index exists to avoid (ADR-05).
"""

import datetime as dt
from dataclasses import dataclass

EDGE_SLACK = dt.timedelta(days=4)
"""How far an edge may sit from the request, beyond one bar, before it counts as a gap.

⚠️ **Without it, the calendar alone would queue collections for ever.** No forex bar can be
stamped on New Year's Day or on a Saturday, so a request starting on either would find the data
"late" by a day or two however many times it was collected — measured here: asked from
2020-01-01, the first EURUSD bar is 2020-01-02 21:00. Four days covers a weekend joined to a
holiday (Good Friday to Monday).

⚠️ **It forgives an edge, never a request that misses the data.** A request that ends before the
first bar or starts after the last one is a gap however close it is: data ending on Friday and a
run over Monday-Tuesday would otherwise be planned as covered and read no bar at all. The price
of that rule is a request made only of a weekend just past the data, which is planned for a
collection that brings nothing — and whose run had no bar to read either way. What remains
forgiven — an edge the request overlaps, missing by less than the slack — makes the run start or
stop that much short, which it reports as its first and last bar.
"""


@dataclass(frozen=True)
class Window:
    """An inclusive span of time: the first instant and the last, as the collector takes them."""

    date_from: dt.datetime
    date_to: dt.datetime


def missing_windows(  # noqa: PLR0913 — keyword-only; each is a separate fact the plan needs
    *,
    date_from: dt.datetime,
    date_to: dt.datetime,
    on_disk: Window | None,
    oldest: dt.datetime | None,
    now: dt.datetime,
    bar: dt.timedelta,
) -> list[Window]:
    """The windows to collect so that `[date_from, date_to]` is on disk, oldest first.

    `on_disk` is the index's extent for this pair, or `None` when it has never been collected.
    `oldest` is the broker's oldest bar for this pair when it has been probed; nothing before it
    is asked for, which is what stops a request older than the broker's history from queueing the
    same useless collection on every run. `now` caps the end: nothing later can exist yet. `bar`
    is the timeframe's length, added to the slack so a weekly bar stamped on Sunday is not late.

    Returns an empty list when nothing needs collecting — including a request that lies wholly
    in the future or wholly before the oldest bar.
    """
    # ⚠️ Years are counted in UTC, because the partitions are. 2022-01-01 00:00 at +03:00 is a
    # trading hour of 2021, and counting its local year would leave that hour out.
    start = _utc(date_from if oldest is None else max(date_from, oldest))
    end = _utc(min(date_to, now))
    if end < start:
        return []

    if on_disk is None:
        return [_whole_years(start.year, end.year, now)]

    first, last = _utc(on_disk.date_from), _utc(on_disk.date_to)
    slack = EDGE_SLACK + bar
    spans: list[tuple[int, int]] = []
    if start < first - slack or end < first:
        spans.append((start.year, first.year))
    # `last` is when the last bar opens; a request starting inside that bar still reads it.
    if end > last + slack or start > last + bar:
        spans.append((last.year, end.year))

    # ⚠️ Joined only when they overlap or touch. Two gaps years apart stay two windows, or the
    # years between them — already on disk — would be downloaded again for nothing.
    merged: list[tuple[int, int]] = []
    for low, high in spans:
        if merged and low <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(merged[-1][1], high))
        else:
            merged.append((low, high))
    return [_whole_years(low, high, now) for low, high in merged]


def _utc(instant: dt.datetime) -> dt.datetime:
    return instant.astimezone(dt.UTC)


def _whole_years(first: int, last: int, now: dt.datetime) -> Window:
    """January 1st of `first` to the end of `last`, in UTC, but never past `now`."""
    return Window(
        dt.datetime(first, 1, 1, tzinfo=dt.UTC),
        min(dt.datetime(last, 12, 31, 23, 59, 59, 999999, tzinfo=dt.UTC), now),
    )

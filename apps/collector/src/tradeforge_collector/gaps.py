"""Missing candles: finding them, and telling the boring ones from the alarming ones.

A gap is a hole in the history, and holes are the quiet way a backtest lies. An hour
missing on a Tuesday in 2021 is an hour the strategy never saw — no signal, no fill,
no drawdown — and nothing in the result says so.

But most holes are not holes. Forex is shut from Friday evening to Sunday evening, so
a year of EURUSD H1 has about fifty *legitimate* multi-day interruptions. A report
that lists all of them buries the one that matters. So every gap gets classified:

* **weekend** — the market was closed. Expected; reported, but not a problem.
* **anomalous** — everything else: a feed outage, a holiday, a symbol whose history
  simply starts later than you asked for. This is the list a human should read.

The rule is a **window**, not a duration, and the difference matters. A gap counts as a
weekend only when *every* missing bar falls inside the interval the forex market is
actually shut: Friday evening through to Monday. Anything that pokes out of that window
— even by one bar — is anomalous.

Two failures this shape avoids:

* A duration ceiling ("under three days is a weekend") cannot work, because a D1
  weekend gap is exactly 72 hours while a Christmas closure is 74 — the two are not
  separable by length.
* "Every missing bar is a Saturday or a Sunday" is too strict for a real feed: forex
  closes around 21:00 UTC on Friday, so the last couple of Friday bars are legitimately
  missing too.

No holiday calendar. Calendars are per-country, per-year, and go stale in silence; a
Friday evening is a Friday evening forever. So Christmas shows up as anomalous — which
is the point. You look at it once, decide it is fine, and move on.
"""

import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass
from itertools import pairwise
from typing import Literal

GapKind = Literal["weekend", "anomalous"]

_FRIDAY = 4
_SATURDAY = 5
_SUNDAY = 6

# Forex settles down around 21:00 to 22:00 UTC on Friday. 20:00 leaves room for brokers
# whose server clock is an hour either side of the pack.
_FRIDAY_CLOSE_HOUR = 20


@dataclass(frozen=True, slots=True)
class Gap:
    """A hole between two consecutive candles."""

    after: dt.datetime
    """The last candle before the hole."""

    before: dt.datetime
    """The first candle after it."""

    missing: int
    """How many bars the timeframe says should have been in between."""

    kind: GapKind

    @property
    def duration(self) -> dt.timedelta:
        return self.before - self.after


def find_gaps(times: Sequence[dt.datetime], step: dt.timedelta) -> list[Gap]:
    """Every interruption in an ascending sequence of candle times.

    `times` must be sorted; a source that returns bars out of order is a bug in the
    source, and quietly sorting here would hide it.
    """
    gaps: list[Gap] = []

    for previous, current in pairwise(times):
        elapsed = current - previous
        if elapsed <= step:
            continue

        missing_times = _missing_between(previous, current, step)
        gaps.append(
            Gap(
                after=previous,
                before=current,
                missing=len(missing_times),
                kind=_classify(missing_times),
            )
        )

    return gaps


def _missing_between(
    previous: dt.datetime, current: dt.datetime, step: dt.timedelta
) -> list[dt.datetime]:
    """The bar openings the timeframe expected and the data does not have."""
    missing: list[dt.datetime] = []
    moment = previous + step
    while moment < current:
        missing.append(moment)
        moment += step
    return missing


def _in_the_weekend_window(moment: dt.datetime) -> bool:
    """Is this an instant at which the forex market is legitimately shut?"""
    weekday = moment.weekday()
    if weekday in (_SATURDAY, _SUNDAY):
        return True
    return weekday == _FRIDAY and moment.hour >= _FRIDAY_CLOSE_HOUR


def _classify(missing_times: Sequence[dt.datetime]) -> GapKind:
    """A weekend is a gap with nothing outside the closure. One stray bar and it is not."""
    return "weekend" if all(map(_in_the_weekend_window, missing_times)) else "anomalous"


def anomalies(gaps: Sequence[Gap]) -> list[Gap]:
    """The gaps a human should actually look at."""
    return [gap for gap in gaps if gap.kind == "anomalous"]


def format_report(gaps: Sequence[Gap], *, limit: int = 20) -> str:
    """A gap report for the terminal. Weekends are counted; anomalies are listed.

    Truncation is announced rather than silent: a report that says "20 gaps" when there
    were 400 is worse than no report, because it reads as good news.
    """
    weekends = [gap for gap in gaps if gap.kind == "weekend"]
    unexpected = anomalies(gaps)

    # ASCII only. A Windows console still speaks cp1252, and an em dash here comes out as
    # a replacement character in the middle of the one line the user is meant to read.
    lines = [
        f"gaps: {len(gaps)} total - {len(weekends)} weekend, {len(unexpected)} anomalous",
    ]

    for gap in unexpected[:limit]:
        lines.append(
            f"  anomalous  {gap.after:%Y-%m-%d %H:%M} -> {gap.before:%Y-%m-%d %H:%M}"
            f"  ({gap.missing} candles missing)"
        )

    if len(unexpected) > limit:
        lines.append(f"  ... and {len(unexpected) - limit} more anomalous gaps")

    return "\n".join(lines)

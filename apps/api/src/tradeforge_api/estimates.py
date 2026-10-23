"""How long a launch will take, measured from what this installation has already done.

His call (22/09): the screen should say how long the **collection** and the **backtests** will
take, each with its own number, beside the count it already shows. The earlier decision stands —
time is shown, never used to refuse (no cap since 18/09).

⚠️ **Measured here, never written down.** A constant ("0.02 s per bar") is right on the machine it
was timed on and silently wrong everywhere else, and it ages without anyone noticing: a faster
engine or a slower broker leaves it plausible and false. So both rates are medians over this
database's own recent history, and an installation with no history gets no estimate rather than
somebody else's. The answer carries how many runs or downloads it rests on, because an estimate
from three runs is a smaller claim than one from two hundred.

⚠️ **Sums, divided by the workers that share them.** The host agent downloads one collection at a
time (`max_jobs = 1`), so a collection estimate is a plain sum. Backtests are synchronous CPU, one
per worker at a time, and since 23/09 compose runs `TRADEFORGE_WORKERS` of them side by side — so
the sum is divided by that count. Each measured run's own duration already carries whatever the
others cost it (they share the machine and the database), so the division is not optimistic twice.

**Backtests are measured per calendar bar**: the window divided by the timeframe's step, weekends
and closures included. That is not the number of candles the engine read, and it does not need to
be — the same definition divides the measured runs and multiplies the requested ones, so the
closures cancel out as long as the markets are alike. What does not cancel is the setup: a
structure setup costs more per bar than an MME9, and one median covers both. An order of
magnitude, and said to be one.
"""

import datetime as dt
from collections import defaultdict
from collections.abc import Iterable
from statistics import median

from sqlalchemy import select
from sqlalchemy.orm import Session

from tradeforge_api.schemas import TimeEstimate
from tradeforge_collector import step
from tradeforge_db.models import Backtest, BacktestStatus, Collection

HISTORY = 200
"""How many of the most recent finished runs, or downloads, a rate is the median of.

Recent rather than all: the engine and the broker both change, and a median over every run ever
made would keep answering for the engine of three months ago."""

type Rate = tuple[float, int]
"""A median rate and how many measurements it is the median of."""

type CollectionRates = tuple[dict[str, Rate], Rate]
"""Seconds per year of download by timeframe, and pooled over every timeframe."""


def calendar_bars(date_from: dt.datetime, date_to: dt.datetime, timeframe: str) -> float:
    """How many bars of `timeframe` fit in the window on the calendar — closures included."""
    return max((date_to - date_from) / step(timeframe), 0.0)


def planned_years(windows: Iterable[tuple[dt.datetime, dt.datetime]]) -> int:
    """Calendar years a plan spans, which is the unit the agent downloads in (`year_slices`)."""
    return sum(date_to.year - date_from.year + 1 for date_from, date_to in windows)


def _seconds(started: dt.datetime, finished: dt.datetime) -> float:
    return (finished - started).total_seconds()


def backtest_rate(session: Session) -> Rate | None:
    """Median seconds per calendar bar over the recent finished runs, and how many it rests on.

    `None` with no usable history. A run is usable when it finished with both instants stamped and
    its window holds at least one bar — a zero-length window would divide by nothing.
    """
    rows = session.execute(
        select(Backtest.started_at, Backtest.finished_at, Backtest.date_from, Backtest.date_to)
        .add_columns(Backtest.timeframe)
        .where(
            Backtest.status == BacktestStatus.DONE,
            Backtest.started_at.is_not(None),
            Backtest.finished_at.is_not(None),
        )
        .order_by(Backtest.finished_at.desc())
        .limit(HISTORY)
    ).all()
    per_bar = [
        _seconds(started, finished) / bars
        for started, finished, date_from, date_to, timeframe in rows
        if started is not None
        and finished is not None
        and (bars := calendar_bars(date_from, date_to, timeframe)) > 0
    ]
    if not per_bar:
        return None
    return median(per_bar), len(per_bar)


def backtests_time(
    session: Session,
    runs: Iterable[tuple[dt.datetime, dt.datetime, str]],
    workers: int = 1,
) -> TimeEstimate | None:
    """How long these runs would take across `workers` running side by side. `None` with no
    history to measure by.

    Each run is (date_from, date_to, timeframe). An empty launch takes no time and still says what
    the rate rests on, so a screen can tell "nothing to run" from "nothing to measure by".
    """
    if workers < 1:
        raise ValueError(f"at least one worker runs the backtests, got {workers}")
    rate = backtest_rate(session)
    if rate is None:
        return None
    seconds_per_bar, based_on = rate
    bars = sum(calendar_bars(start, end, timeframe) for start, end, timeframe in runs)
    return TimeEstimate(seconds=bars * seconds_per_bar / workers, based_on=based_on)


def collection_rates(session: Session) -> CollectionRates | None:
    """Median seconds per year of download, by timeframe and over all of them. `None` with none.

    **By timeframe, because that is what the cost follows**: a year of M1 is sixty times the bars of
    a year of H1, and the terminal fetches history as it is asked (a cold H4 probe took 207 s,
    19/08). The pooled median answers for a timeframe this installation has never downloaded.

    Only downloads that **finished well** count. A failed one stops early, and its time says how
    long it took to fail, not how long a year takes.
    """
    rows = session.execute(
        select(Collection.timeframe, Collection.started_at, Collection.finished_at)
        .add_columns(Collection.years_total)
        .where(
            Collection.status == BacktestStatus.DONE,
            Collection.started_at.is_not(None),
            Collection.finished_at.is_not(None),
            Collection.years_total > 0,
        )
        .order_by(Collection.finished_at.desc())
        .limit(HISTORY)
    ).all()
    by_timeframe: dict[str, list[float]] = defaultdict(list)
    pooled: list[float] = []
    for timeframe, started, finished, years in rows:
        if started is None or finished is None:
            continue
        per_year = _seconds(started, finished) / years
        by_timeframe[timeframe].append(per_year)
        pooled.append(per_year)
    if not pooled:
        return None
    return (
        {tf: (median(values), len(values)) for tf, values in by_timeframe.items()},
        (median(pooled), len(pooled)),
    )


def collection_time(
    rates: CollectionRates | None,
    timeframe: str,
    years: int,
) -> TimeEstimate | None:
    """How long `years` of `timeframe` would take to download, from rates already read."""
    if rates is None:
        return None
    by_timeframe, pooled = rates
    seconds_per_year, based_on = by_timeframe.get(timeframe, pooled)
    return TimeEstimate(seconds=years * seconds_per_year, based_on=based_on)


__all__ = [
    "HISTORY",
    "backtest_rate",
    "backtests_time",
    "calendar_bars",
    "collection_rates",
    "collection_time",
    "planned_years",
]

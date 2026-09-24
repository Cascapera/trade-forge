"""Testing a sweep's best points on a window none of them was chosen on — the choosing and the
comparing, with no database and no HTTP.

A sweep's best run is the best of thousands of draws over one window, and re-running it over the
same window returns the identical number: the engine is deterministic. The only second opinion a
sweep can get is data the winner was not chosen on. Done by hand on 24/09 — the best family's
in-sample median of +50% over six years was +1.3% over the next nine months, and three of the
four families tested went negative — which is the whole case for making it one request.

⚠️ **Chosen per (entry, chart, market), never across them.** Entries are alternative methods, and
the charts and markets are where the question is asked; a single "top N" over the whole sweep
would test the luckiest corner of one entry on one chart and call it the sweep's answer.
"""

import datetime as dt
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from statistics import median

from tradeforge_db.models import BacktestMetrics, SelectionMetric

_METRIC_OF: Mapping[SelectionMetric, Callable[[BacktestMetrics], Decimal | None]] = {
    SelectionMetric.NET_PROFIT: lambda metrics: metrics.net_profit,
    SelectionMetric.PROFIT_FACTOR: lambda metrics: metrics.profit_factor,
    SelectionMetric.SHARPE: lambda metrics: metrics.sharpe,
    SelectionMetric.EXPECTANCY: lambda metrics: metrics.expectancy,
}
"""Which column each ranking reads — spelled out, as the walk-forward spells its own."""


@dataclass(frozen=True, slots=True)
class Candidate:
    """One finished run of the sweep, as the choice reads it."""

    group: tuple[str, str, str]
    """`(entry_id, timeframe, symbol)` — the cell a point competes in."""

    order: int
    """Its place in launch order, which breaks ties: the same data always chooses the same run."""

    metrics: BacktestMetrics


def floor_of(timeframe: str, floors: Mapping[str, int]) -> int:
    """The fewest trades a run needs on this chart to be chosen — never fewer than one.

    The sweep's own floor (`retention.MIN_TRADES`) is zero on H4 and above, which suits deciding
    what to keep and not what to test: a run that never traded ranks on nothing.
    """
    return max(floors.get(timeframe, 0), 1)


def choose(
    candidates: Sequence[Candidate],
    *,
    metric: SelectionMetric,
    top_n: int,
    floors: Mapping[str, int],
) -> list[Candidate]:
    """The `top_n` best of each group by `metric`, among the runs that can be ranked at all.

    ⚠️ **A run with no value for the metric is not ranked, never ranked as zero.** Three of the
    four metrics are nullable — a profit factor with no losing trade, a Sharpe over one trade —
    and a zero would put an unmeasured run above every losing one. The same refusal the
    walk-forward makes (`walkforward.choose`).
    """
    by_group: dict[tuple[str, str, str], list[tuple[Decimal, Candidate]]] = {}
    for one in candidates:
        value = _METRIC_OF[metric](one.metrics)
        if value is None or one.metrics.total_trades < floor_of(one.group[1], floors):
            continue
        by_group.setdefault(one.group, []).append((value, one))
    chosen: list[Candidate] = []
    for ranked in by_group.values():
        ranked.sort(key=lambda pair: (-pair[0], pair[1].order))
        chosen.extend(one for _value, one in ranked[:top_n])
    return sorted(chosen, key=lambda one: one.order)


def overlaps(
    date_from: dt.datetime,
    date_to: dt.datetime,
    searched_from: dt.datetime,
    searched_to: dt.datetime,
) -> bool:
    """Does the test window share any instant with the window the points were chosen on?

    ⚠️ **Any shared bar disqualifies it.** A test window that reaches one day into the searched
    one has bars the winner was chosen on, and its result is partly the in-sample result again.
    Touching ends do not count: a sweep's bars run up to `date_to`, and a test from that instant
    on starts after them.
    """
    return date_from < searched_to and searched_from < date_to


def median_of(values: Sequence[Decimal]) -> Decimal | None:
    """The median, or `None` for nothing to take it of — never a zero that reads as measured."""
    return median(values) if values else None


def positive_share(values: Sequence[Decimal]) -> Decimal | None:
    """The fraction of `values` above zero, or `None` for no values."""
    if not values:
        return None
    return Decimal(sum(1 for value in values if value > 0)) / Decimal(len(values))


__all__ = [
    "Candidate",
    "choose",
    "floor_of",
    "median_of",
    "overlaps",
    "positive_share",
]

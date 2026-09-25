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
from enum import StrEnum
from statistics import median

from tradeforge_db.models import BacktestMetrics

_INFINITY = Decimal("Infinity")


class HoldoutRank(StrEnum):
    """What "best" means when a test picks its points.

    The first four are the walk-forward's (`SelectionMetric`); the rest read the run's risk in R
    (25/09, `r_metrics`). Its own enum rather than more members on `SelectionMetric`, which is a
    column type of `walk_forwards` — adding to it would change that table for a choice it never
    makes. A test's rule is stored as JSON, so its values need no column at all.
    """

    NET_PROFIT = "net_profit"
    PROFIT_FACTOR = "profit_factor"
    SHARPE = "sharpe"
    EXPECTANCY = "expectancy"
    NET_R = "net_r"
    RECOVERY_R = "recovery_r"
    """Net R over the deepest drawdown in R: what the run made per unit of the worst it went
    through."""
    POSITIVE_YEARS = "positive_years"


def recovery_r(metrics: BacktestMetrics) -> Decimal | None:
    """Net R over the deepest drawdown in R.

    ⚠️ **No drawdown is not "no score".** A run that gained and never fell from a peak made
    something for nothing, and is the best there is by this measure (+∞) — the profit factor's
    no-loss case, the same way. A run with no drawdown that made nothing has nothing to say.
    """
    if metrics.net_r is None or metrics.max_drawdown_r is None:
        return None
    if metrics.max_drawdown_r > 0:
        return metrics.net_r / metrics.max_drawdown_r
    return _INFINITY if metrics.net_r > 0 else None


_METRIC_OF: Mapping[HoldoutRank, Callable[[BacktestMetrics], Decimal | None]] = {
    HoldoutRank.NET_PROFIT: lambda metrics: metrics.net_profit,
    HoldoutRank.PROFIT_FACTOR: lambda metrics: metrics.profit_factor,
    HoldoutRank.SHARPE: lambda metrics: metrics.sharpe,
    HoldoutRank.EXPECTANCY: lambda metrics: metrics.expectancy,
    HoldoutRank.NET_R: lambda metrics: metrics.net_r,
    HoldoutRank.RECOVERY_R: recovery_r,
    HoldoutRank.POSITIVE_YEARS: lambda metrics: metrics.positive_year_share,
}
"""Which column each ranking reads — spelled out, as the walk-forward spells its own."""


@dataclass(frozen=True, slots=True)
class Bounds:
    """Filters a run must pass to be ranked at all (25/09). `None` is "no filter".

    ⚠️ **A run without the measure never passes a filter on it.** A run recorded before 25/09
    has no risk in R, and "unknown" is not "within the limit".
    """

    max_drawdown_r: Decimal | None = None
    min_positive_year_share: Decimal | None = None

    def admit(self, metrics: BacktestMetrics) -> bool:
        if self.max_drawdown_r is not None and (
            metrics.max_drawdown_r is None or metrics.max_drawdown_r > self.max_drawdown_r
        ):
            return False
        return self.min_positive_year_share is None or (
            metrics.positive_year_share is not None
            and metrics.positive_year_share >= self.min_positive_year_share
        )


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
    metric: HoldoutRank,
    top_n: int,
    floors: Mapping[str, int],
    bounds: Bounds | None = None,
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
        if bounds is not None and not bounds.admit(one.metrics):
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
    "Bounds",
    "Candidate",
    "HoldoutRank",
    "choose",
    "floor_of",
    "median_of",
    "overlaps",
    "positive_share",
    "recovery_r",
]

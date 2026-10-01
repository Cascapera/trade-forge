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

    `floors` is the ranking floor (`ranking_floor.RANK_MIN_TRADES`, 01/10) with the request's own
    lines over it. Not the keeping floor (`retention.MIN_TRADES`), which is zero on H4 and above:
    that suits deciding what to keep and not what to test. The one-trade minimum stays for a
    request that names a chart with no floor: a run that never traded ranks on nothing.
    """
    return max(floors.get(timeframe, 0), 1)


BEHAVIOUR_FIELDS: tuple[str, ...] = (
    "total_trades",
    "long_trades",
    "short_trades",
    "net_profit",
    "net_r",
    "max_drawdown_r",
    "yearly_r",
)
"""The metrics `behaviour` reads, in its order — the one definition of "the same run" (01/10).

Read by `behaviour` here, by the ranked page of a sweep's runs (`routers.sweeps._clone_ranked`),
which groups on the same columns in SQL, and so by the dataset's `clone_of` through `behaviour`.
⚠️ A field added here is added to all three: two lists would let the page hide a run the
reserved-window test still counts as distinct."""


def behaviour(metrics: BacktestMetrics) -> tuple[object, ...]:
    """What a run did, read from its metrics: two runs with the same trades have the same one.

    Points that differ only in a dial their trades never reached — a target no trade got to, a
    breakeven never armed — make the very same trades, and a sweep ranks them side by side. The
    first real chain (28/09, AUDUSD M5) chose five points of which three were one run: 31 trades,
    7.6959 R, the same R year by year. Read from the metrics because a losing run keeps no trades
    (`retention`), and the choice ranks every run.

    ⚠️ **Exact, not close.** Two runs that share all but one trade differ here and both are kept;
    only an identical record is a clone.
    """
    yearly = metrics.yearly_r or {}
    return tuple(
        tuple(sorted((year, str(value)) for year, value in yearly.items()))
        if name == "yearly_r"
        else getattr(metrics, name)
        for name in BEHAVIOUR_FIELDS
    )


def choose(  # noqa: PLR0913 — keyword-only; each is one part of the rule
    candidates: Sequence[Candidate],
    *,
    metric: HoldoutRank,
    top_n: int,
    floors: Mapping[str, int],
    bounds: Bounds | None = None,
    distinct: bool = True,
) -> list[Candidate]:
    """The `top_n` best of each group by `metric`, among the runs that can be ranked at all.

    ⚠️ **A run with no value for the metric is not ranked, never ranked as zero.** Three of the
    four metrics are nullable — a profit factor with no losing trade, a Sharpe over one trade —
    and a zero would put an unmeasured run above every losing one. The same refusal the
    walk-forward makes (`walkforward.choose`).

    ⚠️ **`distinct` skips a run that did what a better-ranked one already did** (`behaviour`), and
    the next distinct run takes its place (28/09). Tested again, clones return one answer N times,
    and a cluster built from them opens one trade N times over.
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
        seen: set[tuple[object, ...]] = set()
        taken = 0
        for _value, one in ranked:
            if taken == top_n:
                break
            if distinct:
                did = behaviour(one.metrics)
                if did in seen:
                    continue
                seen.add(did)
            chosen.append(one)
            taken += 1
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
    "BEHAVIOUR_FIELDS",
    "Bounds",
    "Candidate",
    "HoldoutRank",
    "behaviour",
    "choose",
    "floor_of",
    "median_of",
    "overlaps",
    "positive_share",
    "recovery_r",
]

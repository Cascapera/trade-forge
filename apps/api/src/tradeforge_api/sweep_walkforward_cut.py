"""A sweep's walk-forward answered by cutting its own runs (01/10) — no database, no HTTP, no run.

The walk-forward that re-runs (`sweep_walkforward`) asks, fold by fold, the whole sweep again on
the training years and the chosen points again on the test years: hours of queue for each fold. A
run of the parent sweep already holds its R by year of entry and year of exit (`r_by_years`) and,
since 01/10, its trades in the same cells (`trades_by_years`). For windows of **whole years inside
the parent's own**, that answers both halves of a fold without running anything (`year_cut`): the
training cut ranks the runs, the test cut of the ones chosen is what they did next.

What the cut can and cannot answer, and so what this mode refuses or leaves out:

* **It ranks by net R and nothing else.** The cut holds R by year and trades by year; a profit
  factor, a Sharpe or a drawdown of the training years would need the trades, which a sweep's
  losing run does not keep. The drawdown limit is refused for the same reason.
* **A run whose cut the guard refuses is left out** (`year_cut.NoCut`: it turned signals away for a
  lot of zero, or would have sized under two steps started later) — counted by reason, never
  ranked on an R a run of the window would not have made.
* **A run without `trades_by_years` is left out** — recorded before 01/10: its R could be cut, but
  the trade floor would stand on nothing.

⚠️ **Clones are read on the whole run** (`holdout.behaviour`), not on the cut: two runs whose
training cuts agree but whose other years differ are two methods, and stay two.
"""

import datetime as dt
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import Any

from tradeforge_api.holdout import floor_of, median_of, positive_share
from tradeforge_api.r_metrics import MIN_YEARS_FOR_SHARE
from tradeforge_api.sweep_walkforward import Window
from tradeforge_api.year_cut import NoCut, YearCut, cut_years

_ZERO = Decimal(0)


class Excluded(StrEnum):
    """Why a run of the parent was not ranked on a fold's training cut."""

    NO_COUNTS = "no_counts"
    """Recorded before it kept its trades by year (`trades_by_years`, 01/10)."""
    REFUSED_CUT = "refused_cut"
    """The guard refused its cut (`year_cut.NoCut`): a run of the window could have traded
    otherwise."""
    UNDER_FLOOR = "under_floor"
    """Fewer trades in the cut than its chart's floor."""
    POSITIVE_YEARS = "positive_years"
    """The cut's share of positive years under the request's, or too few years to have one."""


@dataclass(frozen=True, slots=True)
class CutRun:
    """One finished run of the parent sweep, as the cut reads it."""

    run_id: str
    group: tuple[str, str, str]
    """`(entry_id, timeframe, symbol)` — the cell a run competes in, as in `holdout.Candidate`."""
    order: int
    """Its place in launch order, which breaks ties (`routers.sweeps._runs_of`)."""
    label: str
    """`"{symbol} · {point label}"` — what the screen shows as "chosen"."""
    behaviour: tuple[object, ...]
    """`holdout.behaviour` of the whole run."""
    r_by_years: Mapping[str, Mapping[str, str]] | None
    trades_by_years: Mapping[str, Mapping[str, int]] | None
    sizing_by_years: Mapping[str, Mapping[str, str | None]] | None
    sizing_refusals: int | None
    initial_capital: Decimal
    date_from: dt.datetime
    date_to: dt.datetime


@dataclass(frozen=True, slots=True)
class CutRule:
    """How each fold chooses on its training cut — `CreateSweepWalkForward`'s rule, cut down to
    what a cut can measure."""

    top_n: int
    floors: Mapping[str, int]
    """The ranking floor with the request's own lines over it, as a reserved-window test's."""
    min_positive_year_share: Decimal | None = None
    distinct: bool = True


@dataclass(frozen=True, slots=True)
class Pick:
    """A run chosen on a fold's training cut, and its test cut."""

    run: CutRun
    train: YearCut
    test: YearCut | None
    """`None` when the guard refuses the test years (`test_refused` says why)."""
    test_refused: str | None = None


@dataclass(frozen=True, slots=True)
class CutGroup:
    """One (entry, chart) in one fold: in and out of sample, in R, the way a test reads a group
    (`routers.sweeps.get_holdout`)."""

    entry_id: str
    timeframe: str
    points: int
    in_sample_median_r: Decimal | None
    out_of_sample_median_r: Decimal | None
    out_of_sample_positive: Decimal | None
    no_trades_out: int
    """Chosen runs that made no trade in the test years — out of the median and the share."""
    no_cut_out: int
    """Chosen runs whose test years the guard refused — out of the median and the share."""
    chosen: list[str]


@dataclass(frozen=True, slots=True)
class FoldCut:
    """A fold's whole answer: what was ranked, what was left out and why, and what it chose."""

    candidates: int
    excluded: dict[str, int]
    picks: list[Pick]
    groups: list[CutGroup] = field(default_factory=list)


def years_of(window_from: dt.datetime, window_to: dt.datetime) -> tuple[int, int]:
    """`[first, last]` of a window of whole years — its end is the first instant after it."""
    return window_from.year, window_to.year - 1


def _cut(run: CutRun, first_year: int, last_year: int) -> YearCut:
    return cut_years(
        r_by_years=run.r_by_years,
        sizing_by_years=run.sizing_by_years,
        sizing_refusals=run.sizing_refusals,
        initial_capital=run.initial_capital,
        date_from=run.date_from,
        date_to=run.date_to,
        first_year=first_year,
        last_year=last_year,
        trades_by_years=run.trades_by_years,
    )


def _share(cut: YearCut) -> Decimal | None:
    """The cut's share of positive years — `r_metrics`'s rule on the cut's own years."""
    years = cut.yearly_r.values()
    if len(cut.yearly_r) < MIN_YEARS_FOR_SHARE:
        return None
    return Decimal(sum(1 for r in years if r > _ZERO)) / Decimal(len(cut.yearly_r))


def train_cut(run: CutRun, first_year: int, last_year: int, rule: CutRule) -> YearCut | Excluded:
    """The run's training cut, or why it is not ranked on it."""
    if run.trades_by_years is None:
        return Excluded.NO_COUNTS
    try:
        cut = _cut(run, first_year, last_year)
    except NoCut:
        return Excluded.REFUSED_CUT
    if cut.trades is None or cut.trades < floor_of(run.group[1], rule.floors):
        return Excluded.UNDER_FLOOR
    if rule.min_positive_year_share is not None:
        share = _share(cut)
        # ⚠️ Unknown is not within the limit, as `holdout.Bounds` reads it.
        if share is None or share < rule.min_positive_year_share:
            return Excluded.POSITIVE_YEARS
    return cut


def choose_by_cut(
    runs: Sequence[CutRun], first_year: int, last_year: int, rule: CutRule
) -> tuple[list[tuple[CutRun, YearCut]], dict[str, int]]:
    """The `top_n` best of each group by the training cut's net R, and the runs left out by reason.

    Ties go to launch order; with `distinct`, a run that did what a better-ranked one already did
    (`behaviour`, the whole run) gives its place to the next — `holdout.choose`'s rule."""
    excluded: Counter[str] = Counter()
    by_group: dict[tuple[str, str, str], list[tuple[CutRun, YearCut]]] = {}
    for run in runs:
        found = train_cut(run, first_year, last_year, rule)
        if isinstance(found, Excluded):
            excluded[found.value] += 1
            continue
        by_group.setdefault(run.group, []).append((run, found))
    chosen: list[tuple[CutRun, YearCut]] = []
    for ranked in by_group.values():
        ranked.sort(key=lambda pair: (-pair[1].net_r, pair[0].order))
        seen: set[tuple[object, ...]] = set()
        taken = 0
        for run, cut in ranked:
            if taken == rule.top_n:
                break
            if rule.distinct:
                if run.behaviour in seen:
                    continue
                seen.add(run.behaviour)
            chosen.append((run, cut))
            taken += 1
    chosen.sort(key=lambda pair: pair[0].order)
    return chosen, dict(sorted(excluded.items()))


def fold_cut(runs: Sequence[CutRun], window: Window, rule: CutRule) -> FoldCut:
    """Choose on the training years, read the chosen on the test years, and group them."""
    train_first, train_last = years_of(window.train_from, window.train_to)
    test_first, test_last = years_of(window.test_from, window.test_to)
    chosen, excluded = choose_by_cut(runs, train_first, train_last, rule)
    picks: list[Pick] = []
    for run, train in chosen:
        try:
            picks.append(Pick(run=run, train=train, test=_cut(run, test_first, test_last)))
        except NoCut as refused:
            picks.append(Pick(run=run, train=train, test=None, test_refused=str(refused)))
    return FoldCut(candidates=len(runs), excluded=excluded, picks=picks, groups=groups_of(picks))


def groups_of(picks: Sequence[Pick]) -> list[CutGroup]:
    """Each (entry, chart) of a fold's picks: medians in and out of sample, in R.

    ⚠️ **A test cut with no trade is not a zero** (01/10, as `get_holdout`): out of the median and
    the share, counted apart. A test cut the guard refused is counted apart too."""
    grouped: dict[tuple[str, str], list[Pick]] = {}
    for pick in picks:
        grouped.setdefault((pick.run.group[0], pick.run.group[1]), []).append(pick)
    out: list[CutGroup] = []
    for (entry_id, timeframe), members in sorted(grouped.items()):
        after = [
            pick.test.net_r for pick in members if pick.test is not None and pick.test.trades != 0
        ]
        out.append(
            CutGroup(
                entry_id=entry_id,
                timeframe=timeframe,
                points=len(members),
                in_sample_median_r=median_of([pick.train.net_r for pick in members]),
                out_of_sample_median_r=median_of(after),
                out_of_sample_positive=positive_share(after),
                no_trades_out=sum(
                    1 for pick in members if pick.test is not None and pick.test.trades == 0
                ),
                no_cut_out=sum(1 for pick in members if pick.test is None),
                chosen=[pick.run.label for pick in members],
            )
        )
    return out


def _text(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def fold_document(found: FoldCut) -> dict[str, Any]:
    """A `FoldCut` as `sweep_walk_forward_folds.cut` keeps it — decimals as strings."""
    return {
        "candidates": found.candidates,
        "excluded": dict(found.excluded),
        "chosen": [
            {
                "run_id": pick.run.run_id,
                "entry_id": pick.run.group[0],
                "timeframe": pick.run.group[1],
                "symbol": pick.run.group[2],
                "label": pick.run.label,
                "train_r": str(pick.train.net_r),
                "train_trades": pick.train.trades,
                "test_r": None if pick.test is None else str(pick.test.net_r),
                "test_trades": None if pick.test is None else pick.test.trades,
                "test_refused": pick.test_refused,
            }
            for pick in found.picks
        ],
        "groups": [
            {
                "entry_id": group.entry_id,
                "timeframe": group.timeframe,
                "points": group.points,
                "in_sample_median_r": _text(group.in_sample_median_r),
                "out_of_sample_median_r": _text(group.out_of_sample_median_r),
                "out_of_sample_positive": _text(group.out_of_sample_positive),
                "no_trades_out": group.no_trades_out,
                "no_cut_out": group.no_cut_out,
                "chosen": list(group.chosen),
            }
            for group in found.groups
        ],
    }


def uncovered_years(
    windows: Sequence[Window], date_from: dt.datetime, date_to: dt.datetime
) -> tuple[list[int], list[int]]:
    """The years the folds need that the parent's window lacks: `(outside, partial)` — wholly
    outside it, or only partly inside (a sweep from March has no whole first year).

    A year `y` is whole when the window starts by 1 January `y` and ends by 1 January `y + 1` —
    `year_cut.cut_years`'s own bounds, so a fold this lets through is never refused for them."""
    needed = sorted(
        {year for window in windows for year in range(window.train_from.year, window.test_to.year)}
    )
    outside: list[int] = []
    partial: list[int] = []
    for year in needed:
        opens = dt.datetime(year, 1, 1, tzinfo=dt.UTC)
        closes = dt.datetime(year + 1, 1, 1, tzinfo=dt.UTC)
        if date_to <= opens or date_from >= closes:
            outside.append(year)
        elif date_from > opens or date_to < closes:
            partial.append(year)
    return outside, partial


__all__ = [
    "CutGroup",
    "CutRule",
    "CutRun",
    "Excluded",
    "FoldCut",
    "Pick",
    "choose_by_cut",
    "fold_cut",
    "fold_document",
    "groups_of",
    "train_cut",
    "uncovered_years",
    "years_of",
]

"""Every sweep launched in a window, summarised at once: the dashboard's arithmetic.

Pure functions over plain rows, so the rules are tested by hand-worked numbers rather than
through a database. The router reads the rows; nothing here knows where they came from.

⚠️ **The rules are the study's and the dataset's, not new ones.** A run's return is its net
profit over its own starting capital; it counts as finished once it has metrics; a win rate
means nothing without trades and is left out rather than read as zero; expectancy is a fraction
of the capital. A dashboard that defined any of these again would sooner or later disagree with
the sweep page it summarises, and the reader would have no way to tell which one was right.

⚠️ **One measurement counts once.** The engine is deterministic, so two sweeps that ran the same
document over the same market, chart, window, capital, costs and engine produced the same number
twice. Counted twice, it would pull every median towards whatever was re-run most — measured on
this project: a morning sweep that repeated the H4 half of the night before made 260 of 794 runs
copies, and moved the overall median from -0.98 % to -0.13 %. `distinct` keeps one of each; the
launch counts and the per-sweep timeline still see every run.

⚠️ **Medians lead, and a best is always shown beside its worst.** Every figure here is
in-sample, over every point of every grid — the best run of a dashboard is the best of the
largest search this project has, and so the number least worth believing on its own.
"""

import datetime as dt
from collections import defaultdict
from collections.abc import Callable, Hashable, Iterable, Sequence
from dataclasses import dataclass
from decimal import Decimal

from tradeforge_api.schemas import (
    DashboardRatio,
    DashboardSlice,
    DashboardSweep,
    DashboardTotals,
    SweepRunCounts,
)
from tradeforge_db.models import BacktestStatus


@dataclass(frozen=True, slots=True)
class RunResult:
    """What a finished run measured — only the fields the dashboard reads."""

    net_profit: Decimal
    total_trades: int
    win_rate: Decimal
    profit_factor: Decimal | None
    expectancy: Decimal | None
    max_drawdown_pct: Decimal


@dataclass(frozen=True, slots=True)
class DashboardRun:
    """One run of one sweep, joined to the labels the dashboard groups it by."""

    sweep_id: str
    entry_id: str
    entry_name: str | None
    """Null once the entry has been removed from the shelf."""

    symbol: str
    timeframe: str
    status: BacktestStatus
    initial_capital: Decimal
    measurement: Hashable
    """What makes two runs the same measurement; equal keys are copies. The router builds it from
    everything a deterministic run's result depends on. Required: a default would make every run
    that forgot it a copy of every other."""

    result: RunResult | None
    """Null until the run has metrics — which is what "finished" means here."""

    @property
    def return_(self) -> Decimal | None:
        if self.result is None:
            return None
        return self.result.net_profit / self.initial_capital


@dataclass(frozen=True, slots=True)
class DashboardSweepRow:
    """A sweep as the dashboard lists it: when it was launched and what it swept."""

    sweep_id: str
    created_at: dt.datetime
    entry_names: list[str | None]


def distinct(runs: Iterable[DashboardRun]) -> list[DashboardRun]:
    """One run per measurement, in first-seen order.

    Of several copies the first **finished** one is kept: a copy that failed or is still queued
    says nothing its finished twin does not, and keeping it instead would hide a result that exists.
    """
    kept: dict[Hashable, DashboardRun] = {}
    for run in runs:
        held = kept.get(run.measurement)
        if held is None or (held.result is None and run.result is not None):
            kept[run.measurement] = run
    return list(kept.values())


def median(values: Sequence[Decimal]) -> Decimal | None:
    """The middle value, or the mean of the two middle ones. Null for nothing, never zero."""
    if not values:
        return None
    ordered = sorted(values)
    middle, odd = divmod(len(ordered), 2)
    return ordered[middle] if odd else (ordered[middle - 1] + ordered[middle]) / 2


def summarise(key: str, label: str | None, runs: Sequence[DashboardRun]) -> DashboardSlice:
    """One group of runs — an entry, a market, a chart, or all of them.

    Winners, losers and flat are split by the sign of the net profit, over finished runs only:
    a run still queued has not won or lost anything, and counting it as flat would dilute the
    share that did win.
    """
    returns = [value for run in runs if (value := run.return_) is not None]
    drawdowns = [run.result.max_drawdown_pct for run in runs if run.result is not None]
    return DashboardSlice(
        key=key,
        label=label,
        runs=len(runs),
        finished=len(returns),
        failed=sum(1 for run in runs if run.status == BacktestStatus.FAILED),
        winners=sum(1 for value in returns if value > 0),
        losers=sum(1 for value in returns if value < 0),
        flat=sum(1 for value in returns if value == 0),
        median_return=median(returns),
        mean_return=sum(returns, Decimal(0)) / len(returns) if returns else None,
        best_return=max(returns, default=None),
        worst_return=min(returns, default=None),
        median_drawdown=median(drawdowns),
        worst_drawdown=max(drawdowns, default=None),
    )


def _ratio(values: list[Decimal]) -> DashboardRatio:
    return DashboardRatio(median=median(values), runs=len(values))


def ratios(runs: Iterable[DashboardRun]) -> tuple[DashboardRatio, DashboardRatio, DashboardRatio]:
    """Win rate, profit factor and expectancy per trade — each over the runs where it exists.

    `runs` on each says how many that was, because the three are defined over different sets:
    a win rate needs a trade, a profit factor needs a loss, and a median over fewer runs than
    the headline counts is a smaller claim than it looks.
    """
    win_rates: list[Decimal] = []
    factors: list[Decimal] = []
    expectancies: list[Decimal] = []
    for run in runs:
        result = run.result
        if result is None:
            continue
        if result.total_trades > 0:
            win_rates.append(result.win_rate)
        if result.profit_factor is not None:
            factors.append(result.profit_factor)
        if result.expectancy is not None:
            expectancies.append(result.expectancy / run.initial_capital)
    return _ratio(win_rates), _ratio(factors), _ratio(expectancies)


def _grouped(
    runs: Sequence[DashboardRun],
    key: Callable[[DashboardRun], str],
    label: Callable[[DashboardRun], str | None],
) -> list[DashboardSlice]:
    groups: dict[str, list[DashboardRun]] = defaultdict(list)
    for run in runs:
        groups[key(run)].append(run)
    slices = [summarise(name, label(members[0]), members) for name, members in groups.items()]
    # ⚠️ By the median, never by the best — see the module's note. A group with nothing
    # finished has no median and goes last; the key breaks ties so the order is reproducible.
    return sorted(
        slices,
        key=lambda one: (one.median_return is None, -(one.median_return or 0), one.key),
    )


def by_entry(runs: Sequence[DashboardRun]) -> list[DashboardSlice]:
    return _grouped(runs, lambda run: run.entry_id, lambda run: run.entry_name)


def by_symbol(runs: Sequence[DashboardRun]) -> list[DashboardSlice]:
    return _grouped(runs, lambda run: run.symbol, lambda run: run.symbol)


def by_timeframe(runs: Sequence[DashboardRun]) -> list[DashboardSlice]:
    return _grouped(runs, lambda run: run.timeframe, lambda run: run.timeframe)


def totals(sweeps: Sequence[DashboardSweepRow], runs: Sequence[DashboardRun]) -> DashboardTotals:
    """What was launched (every run) beside what was measured (each measurement once)."""
    counted = dict.fromkeys(BacktestStatus, 0)
    for run in runs:
        counted[run.status] += 1
    measured = distinct(runs)
    finished = [run.result for run in measured if run.result is not None]
    return DashboardTotals(
        sweeps=len(sweeps),
        entries=len({run.entry_id for run in runs}),
        symbols=sorted({run.symbol for run in runs}),
        timeframes=sorted({run.timeframe for run in runs}),
        runs=SweepRunCounts(
            total=len(runs),
            done=counted[BacktestStatus.DONE],
            running=counted[BacktestStatus.RUNNING],
            queued=counted[BacktestStatus.QUEUED],
            failed=counted[BacktestStatus.FAILED],
        ),
        measurements=len(measured),
        trades=sum(result.total_trades for result in finished),
        runs_without_trades=sum(1 for result in finished if result.total_trades == 0),
    )


def per_sweep(
    sweeps: Sequence[DashboardSweepRow], runs: Sequence[DashboardRun]
) -> list[DashboardSweep]:
    """Each sweep in launch order, with its own median — the dashboard's timeline.

    ⚠️ A median **per sweep**, pooling its entries, and it is labelled as such on screen. It
    answers "how did that evening's search go", not "does a method work" — that is `by_entry`.
    """
    mine: dict[str, list[DashboardRun]] = defaultdict(list)
    for run in runs:
        mine[run.sweep_id].append(run)
    out: list[DashboardSweep] = []
    for sweep in sweeps:
        members = mine.get(sweep.sweep_id, [])
        returns = [value for run in members if (value := run.return_) is not None]
        out.append(
            DashboardSweep(
                id=sweep.sweep_id,
                created_at=sweep.created_at,
                entry_names=list(sweep.entry_names),
                runs=len(members),
                finished=len(returns),
                winners=sum(1 for value in returns if value > 0),
                median_return=median(returns),
            )
        )
    return out

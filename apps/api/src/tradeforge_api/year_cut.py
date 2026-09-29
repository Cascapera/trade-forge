"""Cutting a run to a window of whole years in R, without running it again (ADR-0030), and saying
when that cut is not what a run of the window would give — with no database and no HTTP.

A run of `[2020, 2024]` keeps its R by year of entry, then year of exit (`r_by_years`). A run of
`[2022, 2023]` would close the trades that entered from 2022 and left by 2023, so the cut sums those
cells. The warm-up makes the trades line up (measured 29/09: the first year of the later run equal
to the cent in five of five pairs, with a long average of 200).

⚠️ **What still differs is sizing, and only when the lot comes to zero.** Both runs size every trade
at a percent of their own equity, and the later one opens with the initial capital where the longer
one holds whatever it came to. The R of a trade does not depend on its lot — measured on 29/09, not
one trade of the thousands two runs shared differed in R. But a run whose equity fell until 1 % of
it was worth less than one step of lot **turned signals away** (`RefusedBy.SIZING`), freed the slot,
and took other trades from then on: in 2024 of a run that lost ~100 R a year, 244 trades were only
in the longer run and 380 only in the later one.

So a cut is given only when neither run could have turned a signal away:

* the longer run turned none away (`sizing_refusals == 0`) — whatever it traded, it traded all of;
* the later run would have sized every trade of the cut at **two steps of lot or more**. Its equity
  is the longer run's scaled by `initial capital / equity when the cut opens` — a percent of equity
  compounds the same from any start — so its lot is the longer run's lot times that scale. Two steps
  and not one because the scale is not exact: the longer run's lots were floored to the step, and a
  trade open across the cut's first day moves the longer run's equity and not the later one's.

⚠️ **Conservative by construction.** A run that turned one signal away in its last year has no cut at
all, even of years that were right. The runs this refuses are the ones that lost their account,
which a selection does not keep.
"""

import datetime as dt
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal

from tradeforge_engine.domain import ClosedTrade, EquityPoint, Refusal, RefusedBy

LOT_STEP = Decimal("0.01")
"""The step a run's lots are floored to — `PercentRiskManager`'s, which `runner.risk_for` builds
without naming one."""

HEADROOM_STEPS = 2
"""The smallest lot, in steps, the later run must size every trade of the cut at."""

_ZERO = Decimal(0)


@dataclass(frozen=True, slots=True)
class YearSizing:
    """How a run stood when one year of its window opened, and how it sized that year's trades."""

    equity_at_start: Decimal
    """The equity at the close of the last bar before the year — the initial capital for the year
    the run opened in."""
    smallest_volume: Decimal | None
    """The smallest lot a trade that entered in the year was sized at; `None` for a year with no
    trade."""


@dataclass(frozen=True, slots=True)
class Sizing:
    """What a run's sizing says about cutting it: see the module docstring."""

    by_years: dict[int, YearSizing]
    """Per calendar year of the booked window, from the first bar's to the last's."""
    refusals: int
    """Signals turned away because the lot came to zero, in the booked window."""


def sizing_of(
    *,
    trades: Sequence[ClosedTrade],
    equity_curve: Sequence[EquityPoint],
    refusals: Sequence[Refusal],
    initial_capital: Decimal,
) -> Sizing:
    """Fold a finished run into its `Sizing`. `equity_curve` in time order, as the engine hands it.

    ⚠️ One pass over the curve: an M5 run of five years is ~370 thousand points, and this runs for
    every run of a sweep."""
    smallest: dict[int, Decimal] = {}
    for trade in trades:
        year = trade.entry_time.year
        smallest[year] = min(smallest.get(year, trade.volume), trade.volume)

    opening: dict[int, Decimal] = {}
    held = initial_capital
    for point in equity_curve:
        year = point.time.year
        if year not in opening:
            # Every year from the last one seen up to this one opened on what the run held then.
            for missed in range(max(opening, default=year - 1) + 1, year + 1):
                opening[missed] = held
        held = point.equity

    return Sizing(
        by_years={
            year: YearSizing(equity_at_start=equity, smallest_volume=smallest.get(year))
            for year, equity in sorted(opening.items())
        },
        refusals=sum(1 for refusal in refusals if refusal.refused_by is RefusedBy.SIZING),
    )


def sizing_document(sizing: Sizing) -> dict[str, dict[str, str | None]]:
    """`Sizing.by_years` as `backtest_metrics.sizing_by_years` keeps it."""
    return {
        str(year): {
            "equity_at_start": str(year_sizing.equity_at_start),
            "smallest_volume": (
                None if year_sizing.smallest_volume is None else str(year_sizing.smallest_volume)
            ),
        }
        for year, year_sizing in sorted(sizing.by_years.items())
    }


@dataclass(frozen=True, slots=True)
class YearCut:
    """What a run of `[first_year, last_year]` would have made in R, cut from a longer run."""

    first_year: int
    last_year: int
    net_r: Decimal
    yearly_r: dict[int, Decimal]
    """R per year of entry, of the trades the cut closes; a year with none is absent."""


class NoCut(ValueError):  # noqa: N818 — an answer, not a failure: "this run has no such cut"
    """The run cannot answer this window without running it again — and why."""


def cut_years(  # noqa: PLR0913 — keyword-only; each names one thing the cut is judged on
    *,
    r_by_years: Mapping[str, Mapping[str, str]] | None,
    sizing_by_years: Mapping[str, Mapping[str, str | None]] | None,
    sizing_refusals: int | None,
    initial_capital: Decimal,
    date_from: dt.datetime,
    date_to: dt.datetime,
    first_year: int,
    last_year: int,
) -> YearCut:
    """The cut `[first_year, last_year]` of a run over `[date_from, date_to)`, or `NoCut`."""
    if first_year > last_year:
        raise NoCut(f"the cut starts in {first_year}, after it ends in {last_year}")
    if date_from > dt.datetime(first_year, 1, 1, tzinfo=dt.UTC):
        raise NoCut(f"the run starts on {date_from.date()}, after 1 January {first_year}")
    if date_to < dt.datetime(last_year + 1, 1, 1, tzinfo=dt.UTC):
        raise NoCut(f"the run ends on {date_to.date()}, before the end of {last_year}")
    if r_by_years is None or sizing_by_years is None or sizing_refusals is None:
        raise NoCut("the run was recorded before it kept its R and sizing by year")
    if sizing_refusals:
        raise NoCut(
            f"the run turned {sizing_refusals} signal(s) away for a lot of zero: from there it "
            "traded differently from a run started later"
        )

    _check_sizing(sizing_by_years, initial_capital, first_year, last_year)

    yearly: dict[int, Decimal] = {}
    for entered, exits in r_by_years.items():
        entry_year = int(entered)
        if not first_year <= entry_year <= last_year:
            continue
        for left, r in exits.items():
            if int(left) <= last_year:
                yearly[entry_year] = yearly.get(entry_year, _ZERO) + Decimal(r)
    return YearCut(
        first_year=first_year,
        last_year=last_year,
        net_r=sum(yearly.values(), _ZERO),
        yearly_r=dict(sorted(yearly.items())),
    )


def _check_sizing(
    sizing_by_years: Mapping[str, Mapping[str, str | None]],
    initial_capital: Decimal,
    first_year: int,
    last_year: int,
) -> None:
    """`NoCut` unless a run started in `first_year` sizes every trade of the cut at
    `HEADROOM_STEPS` or more — see the module docstring."""
    sizing = {int(year): value for year, value in sizing_by_years.items()}
    opens = sizing.get(first_year)
    opening_equity = None if opens is None else opens.get("equity_at_start")
    if opening_equity is None:
        raise NoCut(f"the run kept no sizing for {first_year}")
    if Decimal(opening_equity) <= _ZERO:
        raise NoCut(f"the run held no equity when {first_year} opened")
    scale = initial_capital / Decimal(opening_equity)
    for year in range(first_year, last_year + 1):
        smallest = (sizing.get(year) or {}).get("smallest_volume")
        if smallest is None:
            continue
        later_lot = Decimal(smallest) * scale
        if later_lot < HEADROOM_STEPS * LOT_STEP:
            raise NoCut(
                f"a run started in {first_year} would have sized a trade of {year} at about "
                f"{later_lot:.4f} lots, under {HEADROOM_STEPS} steps of {LOT_STEP}: it could "
                "have turned signals away that this run took"
            )


__all__ = [
    "HEADROOM_STEPS",
    "LOT_STEP",
    "NoCut",
    "Sizing",
    "YearCut",
    "YearSizing",
    "cut_years",
    "sizing_document",
    "sizing_of",
]

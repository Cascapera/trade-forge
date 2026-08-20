"""How much history a symbol really has — and what is limiting the answer.

A date is not an answer. Measured on this project's own broker on 19/08/2026, the naive reading
of "the oldest bar" is wrong in three different ways at once, and each one flatters a backtest:

* **The terminal caps it.** `terminal_info().maxbars` was 100000, which on M1 is 98 days. That
  is a setting on one machine, not a fact about the broker — and the data was already on disk.
* **The oldest bars are not market.** EURUSD D1 goes back to 1971, twenty-eight years before the
  euro existed, in bars with `high == low` and `tick_volume = 1`. No range means no stop is ever
  touched intrabar and no gap ever happens: a backtest over them looks *better* and is *less*
  validated, which is the exact inversion of what more history is supposed to buy.
* **The oldest costs are invented.** Up to 2009 the broker stamped one constant spread on every
  bar of the year — 40 in 2004, 30 in 2005, 20 from 2006 to 2009 — and from 2010 it varies. A
  run there is not paying that market's cost, it is paying a number somebody typed.

So a probe reports the measurement **and what bounded it**, and the three bounds are independent:
one is about this machine, one about the prices, one about the costs. Collapsing them into a
single date would leave a reader unable to act on any of them.

Everything here is a pure function over bars. The MetaTrader side — walking positions, sampling
years — lives in `mt5_source`; what can go silently wrong lives here, where it runs on Linux.
"""

import datetime as dt
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from tradeforge_collector.source import Candle

__all__ = [
    "FABRICATED_YEAR_THRESHOLD",
    "MIN_SPREAD_SAMPLES",
    "HistoryReport",
    "count_answering",
    "fabricated_fraction",
    "is_fabricated",
    "is_stamped_spread",
    "last_fabricated_year",
    "usable_from",
]

# How many bars a year needs before "the spread never moved" means anything. One bar is
# trivially constant; a handful is a market that barely traded. Twenty is still small enough
# that a thin year is judged, and large enough that the judgement is about the data.
MIN_SPREAD_SAMPLES = 20

# How much of a year has to be untraded before the year is called filler. Measured on EURUSD D1,
# the fractions fall 100%, 69%, 21%, 12% across 1971-74 and then sit at 0% for decades — so
# anything from about 0.2 to 0.6 picks the same boundary, and a half is the middle of that
# plateau rather than a number tuned to it.
FABRICATED_YEAR_THRESHOLD = 0.5


@dataclass(frozen=True, slots=True)
class HistoryReport:
    """What one (symbol, timeframe) can actually offer, and what is limiting it."""

    oldest: dt.datetime | None
    """The oldest bar the terminal will hand over. `None` when it has none at all."""

    bar_count: int

    terminal_maxbars: int
    """The machine's own ceiling, carried so a reader can tell whose limit they are seeing."""

    bar_count_is_a_ceiling: bool
    """⚠️ `True` when `bar_count` is the probe's own bound rather than the data's.

    Seen for real once `maxbars` was raised to 100 million: EURUSD M1 answered every position
    the search asked for and came back as exactly 10,000,000. Without this field that number is
    indistinguishable from a measurement, and it is the one number on the screen a person would
    plan a backtest window around.
    """

    last_fabricated: int | None
    """The most recent year still containing bars nobody traded, or `None` if none does.

    ⚠️ Deliberately **not** "where the instrument becomes real", which the bars cannot say — see
    `last_fabricated_year`. A series can be a reconstruction long after the untraded bars stop.
    """

    first_measured_cost: int | None
    """The first year whose spread was measured rather than stamped, or `None` if unknown."""

    @property
    def capped_by_terminal(self) -> bool:
        """⚠️ `>=`, not `==`, because the two numbers are not counted the same way.

        `bar_count` counts positions and position 0 is the bar still forming, while `maxbars`
        is the terminal's own setting for the series it keeps. A terminal that holds `maxbars`
        *closed* bars alongside the forming one answers one position more than its ceiling, and
        `==` would read that series — the most capped one there is — as uncapped. At the
        ceiling or past it, the honest sentence is the same: this number is your setting
        talking, not your broker.

        The case `>=` deliberately does **not** catch is a series *below* the ceiling. A
        terminal that trimmed further than its setting is indistinguishable from a broker that
        has that much and no more, and guessing between them would put a settings warning on
        data that has no such limit."""
        return self.bar_count >= self.terminal_maxbars > 0


def count_answering(answers: Callable[[int], bool], *, ceiling: int) -> int:
    """How many positions a series has, found by doubling and then bisecting.

    `answers(position)` is "does the terminal hand back a bar at this position?" — the only
    question MetaTrader will answer about depth, since there is no API that states it. Doubling
    first because the answer spans four orders of magnitude across timeframes (259 bars on D1,
    100000 on M1) and a fixed upper bound would either overshoot every D1 or truncate every M1.

    ⚠️ **Asking by position is the only safe way to ask.** The alternative, `copy_rates_from` with
    an ancient date, was measured on this broker: 1970 **crashes the interpreter** (`OSError:
    Errno 22` out of the C extension), and 1990 on an unselected symbol took 166 seconds and then
    failed. Positions cannot be out of range in a way that takes the process with them.

    Returns 0 when position 0 does not answer, which is a symbol the terminal has nothing for —
    an ordinary state, not a failure.

    `ceiling` bounds the doubling so a source that answers every position (a fake that forgot to
    stop, a terminal in a strange mood) cannot spin forever. Hitting it is reported as the
    ceiling, and the caller can tell because the number is exactly round.
    """
    if not answers(0):
        return 0

    low, high = 0, 1
    while high < ceiling and answers(high):
        low, high = high, high * 2
    high = min(high, ceiling)

    # Invariant: `low` answers, `high` does not (or is the ceiling). Narrow until they touch.
    while low + 1 < high:
        middle = (low + high) // 2
        if answers(middle):
            low = middle
        else:
            high = middle

    # `low` is the last position that answered, and positions count from zero.
    return low + 1


def is_fabricated(bar: Candle) -> bool:
    """A bar nothing traded: no range and no ticks. One price written per period.

    `high == low` **and** `tick_volume <= 1`. Both halves are required — a pegged currency
    prints flat bars with hundreds of ticks, and a dead minute prints a real range with one.
    """
    return bar.high == bar.low and bar.tick_volume <= 1


def fabricated_fraction(bars: Sequence[Candle]) -> float:
    """What share of these bars nothing traded. `0.0` for an empty sequence.

    Asked of a **year** rather than of a series, because that is the resolution at which the
    answer is stable. Measured on EURUSD D1: 100% of 1971, 69% of 1972, 21% of 1973, 12% of
    1974, and 0% from the mid-1990s on. A single flat bar means a quiet day; a year that is
    two-thirds flat is a year somebody filled in.
    """
    if not bars:
        return 0.0
    return sum(1 for bar in bars if is_fabricated(bar)) / len(bars)


def last_fabricated_year(fractions: Mapping[int, float], *, threshold: float) -> int | None:
    """The most recent year that still contains fabricated bars, or `None` if none does.

    ⚠️ **This is the limit of what the bars can prove, and the limit is worth stating plainly.**
    A reconstruction that carries plausible prices *and* plausible volumes is invisible here.
    Measured on EURUSD D1: fabricated bars disappear by the mid-1970s, but the euro did not
    exist until 1999, so 1973 to 1998 is a reconstruction — derived from the mark and the ECU —
    that no property of a bar distinguishes from a market. The yearly median tick volume does
    jump 38x at 1999 (156 to 5,907), but the years before it swing between 131 and 2,006 with
    no order, so a threshold there would be a number chosen to fit one symbol.

    So this answers "from when does the series stop containing bars nobody traded", which is
    measurable, and refuses to answer "from when is this instrument real", which is not. The
    second question has an answer a human knows — a listing date — and the screen asks for it
    rather than inventing one.
    """
    fabricated = [year for year, fraction in fractions.items() if fraction >= threshold]
    return max(fabricated) if fabricated else None


def is_stamped_spread(spreads: Sequence[int], *, floating: bool) -> bool | None:
    """Did the broker measure this year's spread, or write one number across all of it?

    `None` means *cannot tell*, and it is returned in two situations that look nothing alike but
    lead to the same honest answer:

    * **Too few bars.** A year with three of them is constant by arithmetic, not by evidence.
    * **⚠️ The instrument is quoted fixed.** `symbol_info.spread_float` false means the broker
      genuinely charges the same spread all the time — this project's old broker did exactly
      that for AAPL. A constant year there is the truth, not a reconstruction, and reporting it
      as invented would put a warning on the one instrument whose costs are most trustworthy.

    Only for a floating instrument with enough bars does a flat year mean the number was typed.
    """
    if not floating:
        return None
    if len(spreads) < MIN_SPREAD_SAMPLES:
        return None
    return min(spreads) == max(spreads)


def usable_from(report: HistoryReport) -> dt.datetime | None:
    """The earliest instant worth backtesting: the later of the two honest floors.

    Untraded bars and typed costs stop at different moments — on EURUSD D1, 1975 and 2009 — and
    a window is only as trustworthy as its weaker half. Taking the later of the two is the whole
    arithmetic; the value of the function is that it is *named*, so no caller has to remember
    which floor dominates.

    ⚠️ A **lower** bound on trust, not an upper one. It says nothing about the years above it,
    and in particular it cannot see a reconstruction that carries plausible prices and volumes.
    A screen that renders this as "usable from" owes the reader that caveat.

    `None` when the report has nothing to stand on, which a caller must not read as "since
    forever".
    """
    # The year *after* the last fabricated one: the fabricated year itself is not usable.
    after_filler = None if report.last_fabricated is None else report.last_fabricated + 1
    floors = [
        floor
        for floor in (_january(after_filler), _january(report.first_measured_cost))
        if floor is not None
    ]
    if not floors:
        return report.oldest
    latest = max(floors)
    # Never earlier than the data actually goes: a cost floor from a year the terminal cannot
    # even reach would invent history.
    return latest if report.oldest is None else max(latest, report.oldest)


def _january(year: int | None) -> dt.datetime | None:
    """A year as an instant. Whole years: that is the resolution both floors were found at."""
    return None if year is None else dt.datetime(year, 1, 1, tzinfo=dt.UTC)

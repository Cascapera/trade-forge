"""A run's risk read in R — the numbers a selection needs and money cannot give (25/09).

The engine's metrics (`tradeforge_engine.metrics`) are in money and in fractions of the balance,
which is what an account feels. They are the wrong unit for **choosing** among runs: the balance
compounds, so the same sequence of trades draws down more money late than early, and two points
tested over different windows cannot be compared at all. R — each trade's net result over the
risk its stop defined (`ClosedTrade.r_multiple`, after costs and swap) — is the same unit on every
trade of every run.

* **Deepest drawdown in R** — the worst peak-to-trough fall of the cumulative R. "How many stops
  in a row, net, before it came back."
* **Losing streak** — the most consecutive losing trades, and separately the deepest such streak
  in R. The count is what a person has to sit through; the R is what it cost.
* **R per calendar year, and the share of years that ended positive** — stability: a run that made
  all of it in one year and gave some back in every other scores the same total as one that made
  a little every year.

⚠️ **Computed while the trades are in memory, for every run.** A sweep's losing run keeps no
trades (`retention`), so none of this could be computed later for exactly the runs a selection
must be judged against — the same reason the target ladder is (`excursion.target_ladder`).

⚠️ **A trade with no stop has no R** and is left out of all of it, never counted as zero.

Lives beside the worker rather than in the engine: these decide which runs to look at, not what a
run did, and nothing in a backtest reads them.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from tradeforge_engine.domain import ClosedTrade

_ZERO = Decimal(0)

MIN_YEARS_FOR_SHARE = 2
"""A share of positive years needs at least this many years with a trade. One year is "100%" or
"0%" on a single draw, which says nothing the total did not."""


@dataclass(frozen=True, slots=True)
class RMetrics:
    """A run's risk and stability in R. See the module docstring for each."""

    net_r: Decimal
    max_drawdown_r: Decimal
    """Deepest fall of the cumulative R from its running peak, as a non-negative number. The peak
    starts at zero: a run whose first trades lose is in drawdown from its first trade."""
    losing_streak: int
    losing_streak_r: Decimal
    """The deepest run of consecutive losing trades, summed — non-positive."""
    yearly_r: dict[int, Decimal]
    """R per calendar year of entry, for the years that had a trade."""
    by_years: dict[int, dict[int, Decimal]]
    """R by year of entry, then year of exit (ADR-0030): a longer run answers a window of whole
    years `[a, b]` with the cells entered from `a` and left by `b` — the trades a run of that
    window would have closed inside it."""
    positive_year_share: Decimal | None
    """Years ending above zero R over years with a trade; `None` below `MIN_YEARS_FOR_SHARE`."""
    trades_by_years: dict[int, dict[int, int]]
    """How many trades make each cell of `by_years` (01/10): a cut of whole years counts its trades
    from the same cells it sums its R from (`year_cut`), so a trade floor reads the cut and not the
    whole run."""


def r_metrics(trades: Sequence[ClosedTrade]) -> RMetrics:
    """Fold a run's trades, in entry order, into its R metrics."""
    scored = sorted(
        (
            (trade.entry_time, trade.r_multiple, trade.exit_time)
            for trade in trades
            if trade.r_multiple is not None
        ),
        key=lambda scored_trade: scored_trade[0],
    )

    total = _ZERO
    peak = _ZERO
    deepest = _ZERO
    streak = 0
    longest = 0
    streak_r = _ZERO
    worst_streak_r = _ZERO
    yearly: dict[int, Decimal] = {}
    by_years: dict[int, dict[int, Decimal]] = {}
    trades_by_years: dict[int, dict[int, int]] = {}

    for entered, r, left in scored:
        total += r
        peak = max(peak, total)
        deepest = max(deepest, peak - total)

        # ⚠️ Strictly below zero. A trade closed at break-even did not lose, and ends a streak.
        if r < _ZERO:
            streak += 1
            streak_r += r
            longest = max(longest, streak)
            worst_streak_r = min(worst_streak_r, streak_r)
        else:
            streak = 0
            streak_r = _ZERO

        yearly[entered.year] = yearly.get(entered.year, _ZERO) + r
        exits = by_years.setdefault(entered.year, {})
        exits[left.year] = exits.get(left.year, _ZERO) + r
        counted = trades_by_years.setdefault(entered.year, {})
        counted[left.year] = counted.get(left.year, 0) + 1

    share = (
        Decimal(sum(1 for value in yearly.values() if value > _ZERO)) / Decimal(len(yearly))
        if len(yearly) >= MIN_YEARS_FOR_SHARE
        else None
    )
    return RMetrics(
        net_r=total,
        max_drawdown_r=deepest,
        losing_streak=longest,
        losing_streak_r=worst_streak_r,
        yearly_r=yearly,
        by_years=by_years,
        positive_year_share=share,
        trades_by_years=trades_by_years,
    )


__all__ = ["MIN_YEARS_FOR_SHARE", "RMetrics", "r_metrics"]

"""Cutting one out-of-sample run into slices — by calendar year or by blocks of trades — and
judging it by how many of them made money, with no database and no HTTP.

A reserved-window test (`holdout`) answers with one number per point: the whole window's return.
One number hides *when* the money was made — a point that earned everything in one lucky year and
lost in every other scores the same as one that earned a little every year. His ask (25/09): look
at the out-of-sample run in pieces, two ways, and decide which way before looking.

* **By calendar year** — the regime question: "it worked every year but 2022". What operating it
  would have felt like.
* **By blocks of N trades** — the sample-size question. A year of 5 trades and a year of 80 weigh
  the same in the calendar view, which flatters whatever happened in the thin year; every block
  here holds the same number of trades, so each one is the same strength of evidence.

⚠️ **Summed in R, never in money.** The run compounds: a late trade is sized on a bigger balance
than an early one, so money would make late slices look better for no reason but order. R — net
profit over the risk the stop defined (`Trade.r_multiple`, after costs and swap) — is the same
unit in the first slice and the last.

⚠️ **Both views come from one run.** Nothing here runs the engine: the trades are already stored
(a reserved-window test keeps them all, win or lose — `retention`), so a second cut of the same
test costs a query.
"""

import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from tradeforge_db.models import SliceMode

MIN_BLOCK_TRADES = 5
"""The smallest block a person may ask for. Below it one trade is a fifth of the block, and a
share of "positive blocks" measures single trades rather than stretches of a method."""

MIN_COUNTED_SLICES = 2
"""A verdict needs at least this many slices. One slice positive is "100% positive" and says
nothing that the whole-window number did not already say."""


@dataclass(frozen=True, slots=True)
class ClosedTrade:
    """One trade as the slicing reads it: when it was decided, and what it made in R."""

    entry_time: dt.datetime
    r: Decimal | None
    """`None` for a trade with no stop — it has no R, and is counted apart, never as zero."""


@dataclass(frozen=True, slots=True)
class Slice:
    """One piece of the run: its span, how many trades fell in it and what they made in R."""

    label: str
    date_from: dt.datetime
    date_to: dt.datetime
    trades: int
    net_r: Decimal
    counted: bool
    """Whether it enters the verdict. A year with no trade, or the last block short of `size`,
    is shown and not counted: "no evidence" is not "a loss", and a half block is half the
    evidence of the others."""


@dataclass(frozen=True, slots=True)
class Verdict:
    """The share of counted slices above zero R, and whether it clears the bar set at launch."""

    counted: int
    positive: int
    share: Decimal | None
    passed: bool


def by_year(
    trades: Sequence[ClosedTrade], date_from: dt.datetime, date_to: dt.datetime
) -> list[Slice]:
    """One slice per calendar year of the window, in order — the empty years included.

    ⚠️ **A trade belongs to the year it was entered**, not the one it closed in: the decision is
    what the year is being judged on, and a position opened on 30 December was 2025's call.
    The first and last slices are clipped to the window, so a window from March says March.
    """
    scored = [one for one in trades if one.r is not None]
    slices: list[Slice] = []
    for year in range(date_from.year, date_to.year + 1):
        start = max(date_from, dt.datetime(year, 1, 1, tzinfo=date_from.tzinfo))
        end = min(date_to, dt.datetime(year + 1, 1, 1, tzinfo=date_from.tzinfo))
        inside = [one for one in scored if one.entry_time.year == year]
        slices.append(
            Slice(
                label=str(year),
                date_from=start,
                date_to=end,
                trades=len(inside),
                net_r=sum((one.r for one in inside if one.r is not None), Decimal(0)),
                counted=len(inside) > 0,
            )
        )
    return slices


def by_blocks(trades: Sequence[ClosedTrade], size: int) -> list[Slice]:
    """Consecutive blocks of `size` trades in entry order; the last one short of `size` is shown
    and not counted.

    ⚠️ **Only trades with an R are cut.** A trade with no stop has no R to add, and letting it
    fill a place in a block would make that block hold fewer measurements than the others.
    """
    if size < 1:
        raise ValueError(f"a block holds at least one trade, got {size}")
    scored = sorted((one for one in trades if one.r is not None), key=lambda one: one.entry_time)
    slices: list[Slice] = []
    for start in range(0, len(scored), size):
        block = scored[start : start + size]
        slices.append(
            Slice(
                label=f"{start + 1}-{start + len(block)}",
                date_from=block[0].entry_time,
                date_to=block[-1].entry_time,
                trades=len(block),
                net_r=sum((one.r for one in block if one.r is not None), Decimal(0)),
                counted=len(block) == size,
            )
        )
    return slices


def verdict(slices: Sequence[Slice], pass_share: Decimal) -> Verdict:
    """How many counted slices made money, and whether that share reaches `pass_share`.

    ⚠️ **Fewer than `MIN_COUNTED_SLICES` never passes.** A run with one full block is "100%
    positive" on one draw; the bar is about consistency, and one draw has none to show.
    ⚠️ **Strictly above zero.** A slice that broke even did not make money.
    """
    counted = [one for one in slices if one.counted]
    positive = sum(1 for one in counted if one.net_r > 0)
    if not counted:
        return Verdict(counted=0, positive=0, share=None, passed=False)
    share = Decimal(positive) / Decimal(len(counted))
    return Verdict(
        counted=len(counted),
        positive=positive,
        share=share,
        passed=len(counted) >= MIN_COUNTED_SLICES and share >= pass_share,
    )


__all__ = [
    "MIN_BLOCK_TRADES",
    "MIN_COUNTED_SLICES",
    "ClosedTrade",
    "Slice",
    "SliceMode",
    "Verdict",
    "by_blocks",
    "by_year",
    "verdict",
]

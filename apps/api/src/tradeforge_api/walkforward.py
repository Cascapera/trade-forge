"""Cutting a period into train→test folds, choosing a winner in each, and scoring the result.

The arithmetic of a walk-forward, with nothing else in it: no database, no HTTP, no engine.
Everything decided here is decided from a list of candle timestamps and a list of numbers, so
the three places this feature can be silently wrong — where a window ends, which point wins a
tie, and what the folds add up to — are testable at their edges without a container running.

**The one property everything else rests on**: a fold's training window must end on a candle
*strictly before* the first candle of its test window. Overlap by a single bar and the
experiment quietly stops being an experiment — the choice would have seen part of what scores
it, which is the same lookahead the engine forbids at the level of a single decision, made
again at the level of a whole study. See `split`.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from decimal import Decimal

MIN_TEST_BARS = 20
"""How few candles a test window may hold before the split is refused.

A budget, not a law — the same kind of number as `grid.MAX_POINTS`. A test window's whole job
is to produce a result that was not searched for, and a window too short to hold a trade
produces zero, which on screen is indistinguishable from a strategy that looked and declined.
That is the vacuous-test failure: a number that means "nothing happened" being read as "it
broke even".

Twenty is chosen to be obviously too small to trust and still large enough that a fold *can*
trade. It is a floor against nonsense, not a claim that twenty bars is enough evidence — the
fold's own `test_bars` is reported so a reader can judge that themselves.
"""

MIN_FOLDS = 2
"""Fewer folds than this and it is not a walk-forward.

One train/test split is a useful thing and not this thing: it produces a single out-of-sample
number and cannot say whether the choice that produced it was stable, which is half of what
this experiment reports. Mirrored as a CHECK on `walk_forwards.folds`, because a rule that
only lives in Python is a rule the next maintenance script breaks.
"""

MAX_FOLDS = 20
"""More folds than this and each test window is a rumour.

The knob cuts both ways and the ceiling is the less obvious half. Many folds make instability
visible, which is the point; but they make it visible by measuring each fold on a shorter
sample, and past some point the wandering being exposed is just the noise of the short window
— the very failure the experiment was built to expose, reintroduced one level up.
"""

MAX_RUNS = 1000
"""Total backtests one walk-forward will execute: `folds x grid points`, plus one per fold.

Measured on this project's own runs, a backtest takes 0.1 to 0.8 seconds, so this is a
worst case of some eight minutes — long enough to be a deliberate act, short enough to sit
through. Refused with the number in the message, never quietly trimmed: half a walk-forward is
a picture of an experiment that was not run, and it looks exactly like one that was.
"""


class WalkForwardError(ValueError):
    """A walk-forward that cannot be cut into folds anyone should draw a conclusion from."""


@dataclass(frozen=True, slots=True)
class Window:
    """A stretch of the dataset, as both the bars counted and the instants they fall on.

    Both, because the two are used by different readers and neither can be derived from the
    other after the fact. `bars` is what the split was *cut from* — equal counts, so the folds
    carry equal evidence. `start`/`end` are what a run can be launched with, since a backtest
    is bounded by dates. A market has weekends and holidays, so equal counts are unequal
    stretches of calendar and equal stretches of calendar are unequal counts; keeping only one
    would leave the other unrecoverable.
    """

    start: dt.datetime
    end: dt.datetime
    bars: int


@dataclass(frozen=True, slots=True)
class Fold:
    """One train→test pair, in the order they happen."""

    index: int
    train: Window
    test: Window


def split(
    times: Sequence[dt.datetime], *, folds: int, train_multiple: int, anchored: bool
) -> list[Fold]:
    """Cut a period into `folds` train→test pairs by **counting candles**, not calendar.

    `times` is the sorted instants of the candles the study covered. The test windows tile the
    end of the period contiguously, so stitching their results back together describes one
    continuous stretch of time in which every decision was made blind.

    Sizing. With `K` folds and a training window `X` times the length of a test window, the
    period divides into `X + K` equal parts: `X` of them make the first training window and
    the remaining `K` are the test windows. Any remainder from the division goes into the
    earliest training window, which is the harmless place to put it — training on a few extra
    bars biases nothing, while a longer *test* window in one fold would silently weight that
    fold's result above its neighbours'.

    ⚠️ **Where each window ends is the whole experiment.** A training window ends on
    `times[b - 1]` and its test window starts on `times[b]` — two different candles. Ending it
    on `times[b]` instead would put one bar in both, and that single overlapping bar is enough
    to let the choice see part of what scores it. Nothing would fail: every run would succeed,
    the report would render, and the out-of-sample number would be a little too good, for ever.
    Both bounds are inclusive because that is how `runner._candles_to_run` clips a window
    (`date_from <= candle.time <= date_to`), and a boundary rule that disagrees with the one
    the runs actually use is a boundary rule that describes nothing.

    `anchored` decides how the training window moves, and the two ask different questions:

    * **rolling** (`False`) — a fixed-length window slides forward, so every fold chooses from
      a sample of the same size. That is what makes the folds comparable *to each other*, which
      is what any statement about stability depends on.
    * **anchored** (`True`) — training starts at the beginning and grows, so later folds choose
      from more evidence than earlier ones. Better use of history, and the price is that a
      parameter that "settles down" over the folds is now ambiguous: it may be the method
      stabilising, or just the later folds having more data to average the noise out of.

    Raises `WalkForwardError` rather than returning a shorter list: a caller that asked for six
    folds and silently received four would compute a stitched result over the wrong period.
    """
    if folds < MIN_FOLDS:
        raise WalkForwardError(f"a walk-forward needs at least {MIN_FOLDS} folds")
    if folds > MAX_FOLDS:
        raise WalkForwardError(f"{folds} folds is over the {MAX_FOLDS} a walk-forward will cut")
    if train_multiple < 1:
        raise WalkForwardError("the training window cannot be shorter than the test window")

    total = len(times)
    parts = train_multiple + folds
    test_bars = total // parts
    if test_bars < MIN_TEST_BARS:
        raise WalkForwardError(
            f"{total} candles cut into {folds} folds with training {train_multiple}x as long "
            f"leaves {test_bars} candles per test window, under the {MIN_TEST_BARS} a fold "
            f"needs to mean anything; collect more history, or ask for fewer folds"
        )

    # Where the first test window opens. Everything before it is the first training window,
    # including the remainder — see the docstring.
    opening = total - folds * test_bars

    return [
        Fold(
            index=index,
            train=_window(times, 0 if anchored else index * test_bars, opening + index * test_bars),
            test=_window(times, opening + index * test_bars, opening + (index + 1) * test_bars),
        )
        for index in range(folds)
    ]


def _window(times: Sequence[dt.datetime], start: int, stop: int) -> Window:
    """Bars `[start, stop)` as a window. `stop` is exclusive, so `times[stop - 1]` is the end.

    The half-open interval is the reason this is a function rather than three lines repeated
    three times: the one bar of difference between `stop` and `stop - 1` is exactly the leak
    `split` exists to avoid, and writing it once means there is one place for it to be right.
    """
    return Window(start=times[start], end=times[stop - 1], bars=stop - start)


@dataclass(frozen=True, slots=True)
class Candidate:
    """One point of a training grid, as far as choosing between them is concerned."""

    coordinates: tuple[int, ...]
    """Where the point sits on the grid, one index per axis. **The tie-break**, and it has to
    be something with a total order that does not depend on how rows came back from the
    database — see `choose`."""

    label: str
    score: Decimal | None
    """The selection metric for this point, or `None` when the metric is undefined for this
    run: Sharpe over zero trades, a profit factor with no losses to divide by, or a run that
    failed and has no metrics at all. **Never zero for any of those** — zero is a measured
    result, and collapsing the two would rank a strategy that never traded alongside one that
    broke even."""

    trades: int


def choose(candidates: Iterable[Candidate]) -> Candidate | None:
    """The point a fold selects from its training grid, or `None` if it cannot select one.

    Three rules, and each closes a way for a walk-forward to report a number that means
    nothing:

    1. **A null score is not a low score, it is no score**, so the point is not eligible. The
       alternative — treating null as zero — would let a strategy that never traded win a fold
       in which everything else lost money, and the fold's honest finding ("nothing worked
       here") would be reported as a choice.
    2. **A point that took no trades is not eligible either**, even when its metric is a
       perfectly real zero. Selecting it makes the test run vacuous: it will trade nothing,
       return nothing, and its zero will sit in the stitched result looking exactly like a
       fold that traded and broke even. Those two need opposite responses from a reader.
    3. **Ties break on the grid coordinates**, taking the point that comes first in the order
       the axes were declared. Not on the label, not on whatever the database returned first.
       Determinism is an invariant of this project, and a tie is not the rare case it sounds
       like — a grid whose points mostly take the same trades produces a lot of them, and the
       two implementations only differ on the runs where the answer matters most.

    Returns `None` rather than raising, because a fold with no eligible point is a **result**,
    not an error: it says the method found nothing worth trading in that window, which is
    exactly the kind of finding this experiment exists to surface.
    """
    # Built as a list of ranking keys rather than filtered and then sorted, so the type checker
    # sees the score narrowed once, here, instead of needing an assertion further down that a
    # later edit could make untrue.
    ranked = [
        (-candidate.score, candidate.coordinates, candidate)
        for candidate in candidates
        if candidate.score is not None and candidate.trades > 0
    ]
    if not ranked:
        return None
    # Negated score first, so the highest wins; coordinates ascending break the tie. One
    # expression rather than two passes, which is what stops a later edit from making the two
    # rules disagree. The candidate itself is never compared — the key stops at the pair.
    return min(ranked, key=lambda row: (row[0], row[1]))[2]


@dataclass(frozen=True, slots=True)
class Outcome:
    """What one fold decided and what it got for it.

    Both returns are **returns**, as a fraction of the capital every run starts with — never
    the selection metric. Selecting by Sharpe and reporting Sharpe would leave a reader unable
    to answer "so what would this have made?", and the two numbers below have to be in the same
    unit to be subtracted at all.
    """

    index: int
    label: str | None
    """`None` when the fold could not choose — see `choose`."""

    in_sample: Decimal | None
    """What the chosen point returned over the window it was chosen on. The promise."""

    out_of_sample: Decimal | None
    """What that same point returned over the window that followed. The delivery.

    `None` covers two different things, told apart by `label`: no choice was made, or the
    choice was made and its test run has not finished (or failed).
    """


@dataclass(frozen=True, slots=True)
class Verdict:
    """What a walk-forward adds up to. The comparison is the product; the rest is context."""

    folds_total: int
    folds_decided: int
    """How many folds had an eligible point to choose. Fewer than `folds_total` is itself a
    finding, and a loud one — it means whole windows in which nothing in the grid traded."""

    folds_scored: int
    """How many folds have a finished out-of-sample run. The denominator of everything below."""

    folds_profitable: int

    in_sample_median: Decimal | None
    out_of_sample_median: Decimal | None
    degradation: Decimal | None
    """`out_of_sample_median - in_sample_median`: how much of the promise survived being made
    blind. **The headline of the whole feature**, and it is normally negative — a small gap is
    a method that generalises, a gap that swallows the entire in-sample result is a grid that
    was fitting noise. Null until at least one fold has both numbers.

    Medians on both sides rather than means, and for the usual reason plus one specific to
    here: with a handful of folds a single spectacular one moves a mean enough to reverse the
    sign of this subtraction, and the fold most likely to be spectacular is the one that got
    lucky — the exact thing being measured.
    """

    compounded: Decimal | None
    """The folds multiplied together: `Π(1 + r) - 1`. What an account would have done trading
    this method and re-choosing its parameters every window.

    ⚠️ **An approximation, and worth knowing which way it bends.** Each test run starts from
    the same `initial_capital`, so each `r` is a fraction of a fixed base rather than of the
    balance the previous fold left behind. Multiplying them models an account that scales its
    position size with its equity, which is what percent-risk sizing does — so the shape is
    right and the arithmetic is not exact. It is reported beside the median rather than instead
    of it precisely because it is the figure a losing streak distorts most.
    """

    distinct_choices: int
    """How many *different* points the folds selected. The stability report, in one number.

    1 means every fold independently arrived at the same parameters — the strongest evidence a
    grid can give that it found something about the method rather than about the sample. A
    number close to `folds_decided` means the winner moved every window, and then even a
    positive out-of-sample result is a result about a moving target: there is no "the
    parameters" to go and trade.
    """


def verdict(outcomes: Sequence[Outcome]) -> Verdict:
    """Fold the per-fold results into the summary. Pure arithmetic over what the runs reported.

    A fold counts as *scored* only when both of its numbers landed. Averaging the in-sample
    side over folds whose test run is still queued would move the comparison every time one
    finished, and a degradation figure that drifts while you watch it is one nobody can quote.
    """
    scored = [
        outcome
        for outcome in outcomes
        if outcome.in_sample is not None and outcome.out_of_sample is not None
    ]
    inside = [outcome.in_sample for outcome in scored if outcome.in_sample is not None]
    outside = [outcome.out_of_sample for outcome in scored if outcome.out_of_sample is not None]

    in_median = _median(inside)
    out_median = _median(outside)

    compounded: Decimal | None = None
    if outside:
        product = Decimal(1)
        for value in outside:
            product *= Decimal(1) + value
        compounded = product - Decimal(1)

    return Verdict(
        folds_total=len(outcomes),
        folds_decided=sum(1 for outcome in outcomes if outcome.label is not None),
        folds_scored=len(scored),
        folds_profitable=sum(1 for value in outside if value > 0),
        in_sample_median=in_median,
        out_of_sample_median=out_median,
        degradation=None if in_median is None or out_median is None else out_median - in_median,
        compounded=compounded,
        # Over every fold that chose, not only the scored ones: a fold whose choice was made
        # says something about stability even if its test run has not finished yet.
        distinct_choices=len({outcome.label for outcome in outcomes if outcome.label is not None}),
    )


def _median(values: Sequence[Decimal]) -> Decimal | None:
    """The middle value, averaging the two middle ones for an even count. `None` for empty.

    `None`, never zero: an empty list is "nothing has landed yet", and zero would be a measured
    result of no profit. Same distinction the study's aggregate draws, for the same reason.
    """
    if not values:
        return None
    ordered = sorted(values)
    middle, odd = divmod(len(ordered), 2)
    if odd:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2


__all__ = [
    "MAX_FOLDS",
    "MAX_RUNS",
    "MIN_FOLDS",
    "MIN_TEST_BARS",
    "Candidate",
    "Fold",
    "Outcome",
    "Verdict",
    "WalkForwardError",
    "Window",
    "choose",
    "split",
    "verdict",
]

"""Cutting folds, choosing a winner, and folding the folds into a verdict.

No database and no HTTP. This is the half of a walk-forward that can be wrong while everything
still works: the runs execute, the report renders, and the out-of-sample number is a lie. Three
ways for that to happen, and there is a test below aimed at each — a training window that
overlaps its test window by one bar, a choice that ranks a null as a zero, and a summary that
averages numbers which have not all landed.
"""

import datetime as dt
from decimal import Decimal
from itertools import pairwise

import pytest

from tradeforge_api.walkforward import (
    MAX_FOLDS,
    MIN_TEST_BARS,
    Candidate,
    Outcome,
    WalkForwardError,
    choose,
    split,
    verdict,
)

_OPEN = dt.datetime(2024, 1, 1, tzinfo=dt.UTC)

# 240 candles, hourly, **with a hole in the middle**. The hole is the whole point of the
# fixture: a market has weekends and holidays, so 26 bars either side of it span wildly
# different amounts of calendar. A splitter that cut by time instead of by bars would hand one
# fold twice the evidence of its neighbour and the rows would still read as an even split.
_GAP_AT = 120
_GAP = dt.timedelta(days=3)


def _candles(count: int = 240) -> list[dt.datetime]:
    times = []
    when = _OPEN
    for index in range(count):
        if index == _GAP_AT:
            when += _GAP
        times.append(when)
        when += dt.timedelta(hours=1)
    return times


# 240 bars, training 3x as long as testing, 6 folds: nine parts of 26, with the 6 left over
# going into the first training window. Written out because these are the numbers every
# assertion below is read against.
_TEST_BARS = 26
_FIRST_TRAIN = 84


def test_the_split_gives_the_folds_asked_for_with_equal_test_windows() -> None:
    """The golden: six folds of 26 test bars each, tiling the end of the period.

    Equal test windows are not cosmetic. Each fold's return is one observation in the stitched
    result, and a fold measured over twice as many bars as its neighbour would carry twice the
    weight while the summary treated the two as equals.
    """
    folds = split(_candles(), folds=6, train_multiple=3, anchored=False)

    assert [fold.index for fold in folds] == [0, 1, 2, 3, 4, 5]
    assert [fold.test.bars for fold in folds] == [_TEST_BARS] * 6


def test_a_training_window_ends_strictly_before_its_test_window_begins() -> None:
    """**The invariant the whole experiment rests on.**

    One shared bar between training and test is enough to let the choice see part of what
    scores it, and nothing anywhere would fail: every run succeeds, the report renders, and the
    out-of-sample figure is a little too good for ever. The mutant this kills is `times[stop]`
    where `_window` writes `times[stop - 1]`.

    Asserted on every fold rather than the first, because the rolling and anchored branches
    compute the training start differently and only the *end* is shared.
    """
    for fold in split(_candles(), folds=6, train_multiple=3, anchored=False):
        assert fold.train.end < fold.test.start


def test_the_test_windows_are_contiguous_and_reach_the_last_candle() -> None:
    """Stitched together the test windows are one unbroken stretch, ending where the data does.

    Both halves matter. A gap between two test windows would mean the stitched result skipped a
    period nobody chose to skip; stopping short of the end would mean the most recent — and
    most relevant — candles were searched over and never tested on. The mutant here is putting
    the division's remainder at the end instead of into the first training window.
    """
    times = _candles()
    folds = split(times, folds=6, train_multiple=3, anchored=False)

    for earlier, later in pairwise(folds):
        assert times.index(later.test.start) == times.index(earlier.test.end) + 1

    assert folds[-1].test.end == times[-1]
    assert sum(fold.test.bars for fold in folds) == 6 * _TEST_BARS


def test_a_rolling_split_holds_the_training_window_at_one_length() -> None:
    """Rolling means every fold chooses from a sample of the same size.

    That is what makes the folds comparable to each other, which is what any claim about the
    stability of a choice depends on. The mutant is the rolling branch starting at bar 0 —
    which is the anchored branch wearing rolling's name, and the fold *count* would not change.
    """
    folds = split(_candles(), folds=6, train_multiple=3, anchored=False)

    assert [fold.train.bars for fold in folds] == [_FIRST_TRAIN] * 6
    # Sliding by exactly one test window, so no training bar is skipped between folds.
    times = _candles()
    assert times.index(folds[1].train.start) == times.index(folds[0].train.start) + _TEST_BARS


def test_an_anchored_split_grows_the_training_window_from_the_same_opening() -> None:
    """Anchored means every fold trains on all the history before its test window."""
    times = _candles()
    folds = split(times, folds=6, train_multiple=3, anchored=True)

    assert {fold.train.start for fold in folds} == {times[0]}
    assert [fold.train.bars for fold in folds] == [
        _FIRST_TRAIN + index * _TEST_BARS for index in range(6)
    ]
    # The test windows are untouched by the choice: only where training *starts* differs, so
    # the two modes remain comparable on the same out-of-sample periods.
    rolling = split(times, folds=6, train_multiple=3, anchored=False)
    assert [fold.test for fold in folds] == [fold.test for fold in rolling]


def test_the_remainder_of_the_division_lands_in_the_first_training_window() -> None:
    """240 into nine parts is 26 with 6 left over, and the 6 go where they bias nothing.

    Training on a few extra bars changes nothing about what a result means. A longer *test*
    window would silently weight one fold above the others in every figure the summary reports.
    """
    folds = split(_candles(240), folds=6, train_multiple=3, anchored=False)

    assert _FIRST_TRAIN == 3 * _TEST_BARS + 6
    assert folds[0].train.bars == _FIRST_TRAIN
    assert all(fold.test.bars == _TEST_BARS for fold in folds)


def test_the_cut_counts_candles_and_not_calendar() -> None:
    """Two windows of identical length in bars, spanning very different amounts of time.

    This is the assertion that a passing suite would still have without the fixture's hole in
    it — and with the hole, cutting by time cannot produce it. The fold that contains the
    three-day gap covers days that its neighbour covers in hours, and both hold 26 bars.
    """
    folds = split(_candles(), folds=6, train_multiple=3, anchored=False)
    spans = [fold.test.end - fold.test.start for fold in folds]

    assert all(fold.test.bars == _TEST_BARS for fold in folds)
    assert max(spans) > min(spans) + _GAP - dt.timedelta(hours=1)


@pytest.mark.parametrize(
    ("folds", "train_multiple", "count", "expected"),
    [
        (1, 3, 240, "at least 2 folds"),
        (MAX_FOLDS + 1, 3, 4000, f"over the {MAX_FOLDS}"),
        (6, 0, 240, "cannot be shorter"),
        (6, 3, 100, f"under the {MIN_TEST_BARS}"),
    ],
)
def test_a_split_that_would_mean_nothing_is_refused(
    folds: int, train_multiple: int, count: int, expected: str
) -> None:
    """Refused loudly, never trimmed to fit.

    A caller that asked for six folds and silently received four would stitch a result over a
    period it never named. The short-window case is the subtle one: a test window too small to
    hold a trade returns zero, and on screen that is indistinguishable from a strategy that
    looked at the window and declined.
    """
    with pytest.raises(WalkForwardError, match=expected):
        split(_candles(count), folds=folds, train_multiple=train_multiple, anchored=False)


# --------------------------------------------------------------------------- #
# Choosing a winner                                                             #
# --------------------------------------------------------------------------- #


def _point(coordinates: tuple[int, ...], score: str | None, trades: int = 5) -> Candidate:
    return Candidate(
        coordinates=coordinates,
        label=f"period={coordinates[0]}",
        score=None if score is None else Decimal(score),
        trades=trades,
    )


def test_the_highest_score_wins() -> None:
    winner = choose([_point((0,), "1.5"), _point((1,), "9.0"), _point((2,), "3.0")])

    assert winner is not None
    assert winner.coordinates == (1,)


def test_a_point_with_no_score_is_not_a_point_that_scored_zero() -> None:
    """**Null is not zero**, and the fixture is arranged so the difference decides the winner.

    Every real score here is negative — the window was bad for every parameter set that traded
    — and one point has no score at all, because its metric is undefined (Sharpe over zero
    trades, a profit factor with no losses to divide by). Read as a zero it would beat all of
    them and be sent out of sample as "the best parameters", and the fold's honest finding —
    nothing worked here — would be reported as a choice.
    """
    winner = choose([_point((0,), "-0.5"), _point((1,), None), _point((2,), "-0.2")])

    assert winner is not None
    assert winner.coordinates == (2,)


def test_a_point_that_took_no_trades_cannot_be_chosen() -> None:
    """Its zero is real, and selecting it would make the test window vacuous.

    A parameter set that never fires wins any window in which everything else lost money. Its
    out-of-sample run then trades nothing and returns zero — which sits in the stitched result
    looking exactly like a fold that traded and broke even. Those two need opposite responses.
    """
    winner = choose([_point((0,), "0.0", trades=0), _point((1,), "-0.2")])

    assert winner is not None
    assert winner.coordinates == (1,)


def test_a_tie_breaks_on_the_grid_coordinates_and_never_on_arrival_order() -> None:
    """Determinism is an invariant, and ties are not rare: a grid whose points mostly take the
    same trades produces them constantly. Fed in both orders, the answer is the same one."""
    forward = [_point((0, 1), "4.0"), _point((1, 0), "4.0"), _point((0, 0), "4.0")]

    assert choose(forward) == choose(list(reversed(forward)))
    winner = choose(forward)
    assert winner is not None
    assert winner.coordinates == (0, 0)


def test_a_fold_with_nothing_eligible_chooses_nothing() -> None:
    """`None`, not an exception: a window in which nothing traded is a finding, not a failure."""
    assert choose([_point((0,), None), _point((1,), "2.0", trades=0)]) is None
    assert choose([]) is None


# --------------------------------------------------------------------------- #
# The verdict                                                                   #
# --------------------------------------------------------------------------- #


def _outcome(index: int, label: str | None, inside: str | None, outside: str | None) -> Outcome:
    return Outcome(
        index=index,
        label=label,
        in_sample=None if inside is None else Decimal(inside),
        out_of_sample=None if outside is None else Decimal(outside),
    )


def test_the_verdict_reports_how_much_of_the_promise_survived() -> None:
    """The headline: in-sample median against out-of-sample median, and the gap between them.

    The numbers are chosen so mean and median disagree — one fold returned 0.90 in sample,
    which drags the mean to 0.37 against a median of 0.20. Reporting the mean would let a
    single lucky fold decide the sign of the subtraction, and the lucky fold is exactly what is
    being measured.
    """
    result = verdict(
        [
            _outcome(0, "period=9", "0.20", "0.05"),
            _outcome(1, "period=9", "0.10", "-0.02"),
            _outcome(2, "period=20", "0.90", "0.01"),
        ]
    )

    assert result.in_sample_median == Decimal("0.20")
    assert result.out_of_sample_median == Decimal("0.01")
    assert result.degradation == Decimal("0.01") - Decimal("0.20")
    assert result.degradation < 0
    assert result.folds_profitable == 2


def test_the_folds_compound_rather_than_add() -> None:
    """`Π(1 + r) - 1`, because a return is a ratio and ratios do not add.

    Two folds of 50% are not a 100% year, they are 125%. Written with values where the two
    disagree, since the usual small returns make sum and product agree to three decimals — and
    a test built on those would pass against a `sum`.
    """
    result = verdict([_outcome(0, "a", "0.1", "0.5"), _outcome(1, "a", "0.1", "0.5")])

    assert result.compounded == Decimal("1.5") * Decimal("1.5") - Decimal(1)
    assert result.compounded == Decimal("1.25")
    # What a `sum` would have produced, spelled out so the separation is visible rather than
    # inferred: the two only diverge because the returns here are large.
    assert result.compounded != Decimal("0.5") + Decimal("0.5")


def test_a_fold_whose_test_has_not_landed_is_not_scored() -> None:
    """A half-arrived fold is excluded from **both** sides, not just from the one it is missing.

    Counting its in-sample number while its out-of-sample is still queued would move the
    degradation figure every time a run finished — a headline that drifts while you watch it is
    one nobody can quote. The fold still counts as *decided*, because a choice was made.
    """
    result = verdict(
        [
            _outcome(0, "period=9", "0.20", "0.05"),
            _outcome(1, "period=20", "0.80", None),
        ]
    )

    assert result.folds_total == 2
    assert result.folds_decided == 2
    assert result.folds_scored == 1
    assert result.in_sample_median == Decimal("0.20")


def test_a_fold_that_could_not_choose_is_counted_but_not_averaged() -> None:
    result = verdict([_outcome(0, "period=9", "0.20", "0.05"), _outcome(1, None, None, None)])

    assert result.folds_total == 2
    assert result.folds_decided == 1
    assert result.folds_scored == 1


def test_the_verdict_counts_distinct_choices_and_not_choices() -> None:
    """The stability report. Three folds that all picked `period=9` is one choice, not three.

    1 is the strongest evidence a grid can give that it found something about the method rather
    than about the sample. A number near the fold count means the winner moved every window,
    and then there is no "the parameters" to go and trade — whatever the returns say.
    """
    stable = verdict([_outcome(index, "period=9", "0.2", "0.1") for index in range(3)])
    wandering = verdict([_outcome(index, f"period={index}", "0.2", "0.1") for index in range(3)])

    assert stable.distinct_choices == 1
    assert wandering.distinct_choices == 3


def test_nothing_landed_yet_reads_as_null_and_never_as_zero() -> None:
    """Zero is a measured result of no profit; null is an absence of measurement."""
    result = verdict([_outcome(0, None, None, None), _outcome(1, None, None, None)])

    assert result.in_sample_median is None
    assert result.out_of_sample_median is None
    assert result.degradation is None
    assert result.compounded is None
    assert result.folds_profitable == 0

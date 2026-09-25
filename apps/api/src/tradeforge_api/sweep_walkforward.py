"""A sweep's walk-forward: the windows, and what the folds say together — no database, no HTTP.

His ask (25/09, path B): a reserved-window test (`holdout`) asks whether the points chosen *today*
hold on other data. A walk-forward asks the stronger question — whether **the way of choosing**
holds: at each fold the whole sweep is run again on a training window, its best points are chosen
by the test's own rule, and those points are run on the window right after. A method whose choices
keep working fold after fold is one whose selection can be trusted; one whose winners change every
fold and fail out of sample is one that was fitting noise.

Windows are whole calendar years, like the collector's partitions and the slicing by year:

* **Anchored** — every training window starts at `start` and grows: the method sees all the
  history before each test, the way it would be used for real.
* **Rolling** — every training window is `train_years` long and moves: an old regime ages out,
  which is the question when markets change.

The tests tile one after the other, never overlapping, and each starts the instant its training
ends — so no bar a point was chosen on is a bar it is judged on.
"""

import datetime as dt
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

MIN_FOLDS = 2
MAX_FOLDS = 20


@dataclass(frozen=True, slots=True)
class Window:
    train_from: dt.datetime
    train_to: dt.datetime
    test_from: dt.datetime
    test_to: dt.datetime


def _year(year: int) -> dt.datetime:
    return dt.datetime(year, 1, 1, tzinfo=dt.UTC)


def windows(
    *, start_year: int, train_years: int, test_years: int, folds: int, anchored: bool
) -> list[Window]:
    """`folds` train/test windows of whole calendar years, the first training from `start_year`.

    Fold `k` tests `[start + train + k·test, start + train + (k+1)·test)`; its training ends where
    that test starts, and begins at `start` (anchored) or `train_years` before (rolling).
    """
    if train_years < 1 or test_years < 1:
        raise ValueError("a window is at least one year")
    if not MIN_FOLDS <= folds <= MAX_FOLDS:
        raise ValueError(f"a walk-forward has {MIN_FOLDS} to {MAX_FOLDS} folds, not {folds}")
    out: list[Window] = []
    for k in range(folds):
        test_from = start_year + train_years + k * test_years
        out.append(
            Window(
                train_from=_year(start_year if anchored else test_from - train_years),
                train_to=_year(test_from),
                test_from=_year(test_from),
                test_to=_year(test_from + test_years),
            )
        )
    return out


@dataclass(frozen=True, slots=True)
class FoldGroup:
    """One (entry, chart) in one fold's test: its median out of sample and what it chose."""

    fold: int
    median_return: Decimal | None
    positive_share: Decimal | None
    chosen: Sequence[str]
    """The labels of the points this fold chose — one per market and point."""


@dataclass(frozen=True, slots=True)
class Stability:
    folds: int
    """Folds that tested this group."""
    positive_folds: int
    """Folds whose median out-of-sample return was above zero."""
    most_chosen: str | None
    most_chosen_folds: int
    """In how many folds the most chosen point was among the chosen."""


def stability(groups: Sequence[FoldGroup]) -> Stability:
    """How a group held across folds: how often its test median was positive, and whether the
    same point kept being chosen — a method that picks a different winner every fold is one
    whose winners were noise, however well each did."""
    seen = Counter(label for group in groups for label in set(group.chosen))
    most = seen.most_common(1)
    return Stability(
        folds=len(groups),
        positive_folds=sum(
            1 for group in groups if group.median_return is not None and group.median_return > 0
        ),
        most_chosen=most[0][0] if most else None,
        most_chosen_folds=most[0][1] if most else 0,
    )


__all__ = [
    "MAX_FOLDS",
    "MIN_FOLDS",
    "FoldGroup",
    "Stability",
    "Window",
    "stability",
    "windows",
]

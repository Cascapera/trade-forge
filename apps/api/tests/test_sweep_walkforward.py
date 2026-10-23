"""A sweep's walk-forward windows and stability — by hand."""

import datetime as dt
from decimal import Decimal

import pytest

from tradeforge_api.sweep_walkforward import FoldGroup, stability, windows


def year(y: int) -> dt.datetime:
    return dt.datetime(y, 1, 1, tzinfo=dt.UTC)


def test_anchored_training_grows_from_the_start_and_the_tests_tile() -> None:
    """From 2009, 6 years of training, 2 of test, 3 folds: tests 2015-17, 2017-19, 2019-21."""
    found = windows(start_year=2009, train_years=6, test_years=2, folds=3, anchored=True)

    assert [(w.train_from, w.train_to, w.test_from, w.test_to) for w in found] == [
        (year(2009), year(2015), year(2015), year(2017)),
        (year(2009), year(2017), year(2017), year(2019)),
        (year(2009), year(2019), year(2019), year(2021)),
    ]


def test_rolling_training_keeps_its_length_and_moves() -> None:
    found = windows(start_year=2009, train_years=6, test_years=2, folds=3, anchored=False)

    assert [(w.train_from, w.train_to) for w in found] == [
        (year(2009), year(2015)),
        (year(2011), year(2017)),
        (year(2013), year(2019)),
    ]


def test_no_test_bar_is_a_training_bar_of_its_own_fold() -> None:
    for w in windows(start_year=2010, train_years=3, test_years=1, folds=5, anchored=True):
        assert w.train_to == w.test_from


@pytest.mark.parametrize("folds", [1, 21])
def test_the_number_of_folds_is_bounded(folds: int) -> None:
    with pytest.raises(ValueError, match="folds"):
        windows(start_year=2009, train_years=6, test_years=2, folds=folds, anchored=True)


def test_a_window_is_at_least_a_year() -> None:
    with pytest.raises(ValueError, match="at least one year"):
        windows(start_year=2009, train_years=0, test_years=2, folds=2, anchored=True)


def test_stability_counts_positive_folds_and_the_point_chosen_most() -> None:
    groups = [
        FoldGroup(fold=0, median_return=Decimal("0.02"), positive_share=None, chosen=["a", "b"]),
        FoldGroup(fold=1, median_return=Decimal("-0.01"), positive_share=None, chosen=["a", "c"]),
        FoldGroup(fold=2, median_return=Decimal("0.03"), positive_share=None, chosen=["a"]),
        FoldGroup(fold=3, median_return=None, positive_share=None, chosen=["d"]),
    ]

    found = stability(groups)

    assert (found.folds, found.positive_folds) == (4, 2)
    assert (found.most_chosen, found.most_chosen_folds) == ("a", 3)


def test_a_point_chosen_twice_in_one_fold_counts_that_fold_once() -> None:
    """Chosen on two markets in the same fold is still one fold of agreement."""
    found = stability(
        [FoldGroup(fold=0, median_return=None, positive_share=None, chosen=["a", "a"])]
    )

    assert found.most_chosen_folds == 1

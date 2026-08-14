"""The dispersion arithmetic of a study, with no database in sight.

`_aggregate` is the only place in this feature that computes rather than plumbs, so it is
tested directly on in-memory rows. Driving it through HTTP and a real Postgres would run the
same arithmetic behind three layers that can only obscure which one was wrong.

What it is asked to prove is narrower than a basket's, and sharper: a grid **always** has a
best point, including a grid of noise, so the tests here are mostly about the summary refusing
to be the maximum.
"""

from decimal import Decimal

from tradeforge_api.routers.studies import _aggregate
from tradeforge_db.models import Backtest, BacktestMetrics, BacktestStatus

CAPITAL = Decimal("10000")


def _point(
    label: str, net_profit: str | None, status: BacktestStatus = BacktestStatus.DONE
) -> tuple[Backtest, str]:
    """A run carrying only what the aggregate reads. `net_profit=None` means it has no metrics."""
    run = Backtest(status=status)
    run.metrics = None if net_profit is None else BacktestMetrics(net_profit=Decimal(net_profit))
    return run, label


def test_a_grid_that_has_not_started_reports_undefined_rather_than_zero() -> None:
    """Nothing has finished, so there is no return to report — and 0% is a different claim.

    Zero would rank a study nobody has run alongside one where every parameter set broke even.
    The same distinction the spread column makes between NULL and zero, two layers up.
    """
    points = [
        _point("period=5", None, BacktestStatus.QUEUED),
        _point("period=9", None, BacktestStatus.QUEUED),
    ]

    aggregate = _aggregate(points, CAPITAL)

    assert aggregate.points_total == 2
    assert aggregate.points_finished == 0
    assert aggregate.points_profitable == 0
    assert aggregate.median_return is None
    assert aggregate.best_return is None
    assert aggregate.best_label is None


def test_a_run_marked_done_before_its_metrics_exist_is_not_counted_as_finished() -> None:
    """The gap between the status flipping and the metrics row landing is real, and reading
    `net_profit` off it would be an exception raised by a reporting endpoint.

    Stated with a *finished* point beside it, so the test cannot pass by the aggregate simply
    ignoring everything.
    """
    points = [_point("period=5", None, BacktestStatus.DONE), _point("period=9", "500")]

    aggregate = _aggregate(points, CAPITAL)

    assert aggregate.points_total == 2
    assert aggregate.points_finished == 1
    assert aggregate.best_label == "period=9"


def test_the_headline_is_the_median_not_the_best_corner() -> None:
    """⚠️ The test this whole feature exists to satisfy.

    Nine points, one of them spectacular and the rest mediocre — which is what a grid over
    noise looks like. The median says +1%, which is what a parameter set chosen without
    hindsight would have returned. The best says +80%, and reporting that alone would present
    a lucky corner as the strategy's result.

    The numbers are chosen so the mean cannot stand in: the mean of these is ~9.9%, far from
    the median's 1%. A gentler spread would let `mean` pass this test unnoticed.
    """
    points = [
        _point(f"period={p}", profit)
        for p, profit in zip(
            range(1, 10),
            ["-200", "-100", "0", "50", "100", "150", "200", "300", "8000"],
            strict=True,
        )
    ]

    aggregate = _aggregate(points, CAPITAL)

    assert aggregate.median_return == Decimal("0.01")
    assert aggregate.best_return == Decimal("0.8")
    assert aggregate.best_label == "period=9"


def test_an_even_count_averages_the_two_middle_points() -> None:
    """The other branch of the median.

    ⚠️ Four values whose median and mean are **far** apart: median 3.5%, mean 13%. The obvious
    fixture — a tidy symmetric spread — is the one shape where the two statistics agree, and a
    test written on it would be the only test of this branch while proving nothing about it.
    """
    points = [
        _point("a", "-100"),
        _point("b", "200"),
        _point("c", "500"),
        _point("d", "4600"),
    ]

    aggregate = _aggregate(points, CAPITAL)

    assert aggregate.median_return == Decimal("0.035")
    assert aggregate.points_finished == 4


def test_how_much_of_the_searched_space_works_is_reported_beside_the_best() -> None:
    """Three profitable points out of ten is a corner, whatever the best of those three did.

    ⚠️ Break-even counts as **not** profitable. A point that returned exactly nothing did not
    work; folding it in would inflate the one number a reader uses to judge whether the method
    has anything, and the boundary is where an off-by-one in a comparison lives.
    """
    points = [
        _point("a", "-500"),
        _point("b", "-100"),
        _point("c", "0"),
        _point("d", "100"),
        _point("e", "900"),
    ]

    aggregate = _aggregate(points, CAPITAL)

    assert aggregate.points_profitable == 2
    assert aggregate.points_finished == 5


def test_the_extremes_are_named_so_the_corner_can_be_found_and_looked_at() -> None:
    """Both ends carry their label. A return with no name is a number nobody can act on — the
    reader's next move is to open that run, and they need to know which one it is."""
    points = [_point("period=5", "-2500"), _point("period=9", "100"), _point("period=20", "3000")]

    aggregate = _aggregate(points, CAPITAL)

    assert (aggregate.worst_label, aggregate.worst_return) == ("period=5", Decimal("-0.25"))
    assert (aggregate.best_label, aggregate.best_return) == ("period=20", Decimal("0.3"))


def test_a_failed_point_is_counted_without_being_scored() -> None:
    """A parameter set that could not run is not a parameter set that lost money.

    Counting it as a loss would make an invalid corner of the grid look like evidence about the
    market; dropping it silently would make `points_total` disagree with what a reader can see.
    """
    points = [
        _point("period=5", "1000"),
        _point("period=0", None, BacktestStatus.FAILED),
        _point("period=9", "-500"),
    ]

    aggregate = _aggregate(points, CAPITAL)

    assert aggregate.points_total == 3
    assert aggregate.points_finished == 2
    assert aggregate.points_failed == 1
    assert aggregate.median_return == Decimal("0.025")

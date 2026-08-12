"""The dispersion arithmetic of a basket, with no database in sight.

`_aggregate` is the only place in this feature that computes rather than plumbs, so it is
tested directly on in-memory rows. Driving it through HTTP and a real Postgres would run the
same arithmetic behind three layers that can only obscure which one was wrong.
"""

from decimal import Decimal

from tradeforge_api.routers.baskets import _aggregate
from tradeforge_db.models import Backtest, BacktestMetrics, BacktestStatus

CAPITAL = Decimal("10000")


def _run(
    symbol: str, net_profit: str | None, status: BacktestStatus = BacktestStatus.DONE
) -> tuple[Backtest, str]:
    """A run carrying only what the aggregate reads. `net_profit=None` means it has no metrics."""
    run = Backtest(status=status)
    run.metrics = None if net_profit is None else BacktestMetrics(net_profit=Decimal(net_profit))
    return run, symbol


def test_a_basket_whose_runs_are_all_queued_reports_undefined_rather_than_zero() -> None:
    """Nothing has finished, so there is no return to report — and 0% is a different claim.

    Zero would rank a basket that has not started alongside one that broke even on every
    market. The same distinction the spread column makes between NULL and zero, one layer up.
    """
    runs = [_run("EURUSD", None, BacktestStatus.QUEUED), _run("AAPL", None, BacktestStatus.QUEUED)]

    aggregate = _aggregate(runs, CAPITAL)

    assert aggregate.runs_total == 2
    assert aggregate.runs_finished == 0
    assert aggregate.runs_profitable == 0
    assert aggregate.best_return is None
    assert aggregate.worst_return is None
    assert aggregate.median_return is None
    assert aggregate.best_symbol is None


def test_the_extremes_are_named_so_the_market_that_breaks_the_strategy_is_visible() -> None:
    """The point of a basket: +30% somewhere and -25% elsewhere is not "a strategy that works"."""
    runs = [
        _run("EURUSD", "-2500"),
        _run("AAPL", "3000"),
        _run("GBPUSD", "500"),
    ]

    aggregate = _aggregate(runs, CAPITAL)

    assert aggregate.best_symbol == "AAPL"
    assert aggregate.best_return == Decimal("0.3")
    assert aggregate.worst_symbol == "EURUSD"
    assert aggregate.worst_return == Decimal("-0.25")


def test_the_middle_is_the_median_so_one_spectacular_market_cannot_drag_it() -> None:
    """The test that fails if someone reaches for `mean` — and the numbers are chosen to say so.

    Four runs: three flat losses and one huge winner. The **mean** is +18.75%, which reads as a
    strong strategy. The **median** is -1%, which is the truth about the typical market. Any
    implementation using an average reports a positive middle here.
    """
    runs = [
        _run("EURUSD", "-100"),
        _run("GBPUSD", "-100"),
        _run("US500", "-100"),
        _run("AAPL", "7800"),
    ]

    aggregate = _aggregate(runs, CAPITAL)

    assert aggregate.median_return == Decimal("-0.01")
    assert aggregate.best_symbol == "AAPL"


def test_an_odd_count_takes_the_middle_value_itself() -> None:
    runs = [_run("A", "-300"), _run("B", "100"), _run("C", "900")]

    assert _aggregate(runs, CAPITAL).median_return == Decimal("0.01")


def test_an_even_count_averages_the_two_middle_values() -> None:
    """Chosen so the answer is a value no single run has: an off-by-one picks 1% or 3%."""
    runs = [_run("A", "-500"), _run("B", "100"), _run("C", "300"), _run("D", "900")]

    assert _aggregate(runs, CAPITAL).median_return == Decimal("0.02")


def test_a_breakeven_run_is_not_counted_as_profitable() -> None:
    """Exactly zero is the boundary, and `>= 0` here would report a losing basket as half-won.

    A run that returned nothing did not profit. Worth its own test because the boundary is a
    single character and every other assertion in this file passes with it wrong.
    """
    runs = [_run("A", "0"), _run("B", "1"), _run("C", "-1")]

    assert _aggregate(runs, CAPITAL).runs_profitable == 1


def test_a_failed_run_is_counted_as_failed_and_excluded_from_the_statistics() -> None:
    """A run that crashed is not a market where the strategy lost money.

    Folding it into the returns would invent a 0% result for a market that was never measured,
    and the basket would report better dispersion than it earned.
    """
    runs = [
        _run("EURUSD", "1000"),
        _run("GBPUSD", None, BacktestStatus.FAILED),
        _run("AAPL", "-1000"),
    ]

    aggregate = _aggregate(runs, CAPITAL)

    assert aggregate.runs_total == 3
    assert aggregate.runs_finished == 2
    assert aggregate.runs_failed == 1
    assert aggregate.median_return == Decimal("0")


def test_a_run_marked_done_without_metrics_yet_is_not_read() -> None:
    """`status == done` and "has metrics" are written in that order, so there is a gap.

    Reading `net_profit` off the row during it would be an AttributeError inside a reporting
    endpoint. The aggregate keys off the metrics, not the status.
    """
    runs = [_run("EURUSD", "1000"), _run("AAPL", None, BacktestStatus.DONE)]

    aggregate = _aggregate(runs, CAPITAL)

    assert aggregate.runs_finished == 1
    assert aggregate.runs_failed == 0
    assert aggregate.best_symbol == "EURUSD"

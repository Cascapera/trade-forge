"""The two places a walk-forward reads a number off a finished run.

No database and no HTTP — ORM rows built in memory, which is all these two need. The arithmetic
of the folds lives in `test_walkforward.py`; the wiring against Postgres lives in
`test_walkforwards_integration.py`. What is left is small and is exactly where a wrong number
would be invisible: reading the *wrong column* for a metric, and turning a run that has not
finished into a zero.
"""

from decimal import Decimal

import pytest

from tradeforge_api.routers.walkforwards import _return_of
from tradeforge_api.worker import _METRIC_OF
from tradeforge_db.models import Backtest, BacktestMetrics, SelectionMetric


def _metrics(**overrides: object) -> BacktestMetrics:
    """A finished run's metrics, with every nullable column filled unless a test empties it.

    Every value below is **distinct**, and that is the point rather than tidiness: a mapping
    that read `net_profit` for every metric would pass against a fixture where two of them
    happened to be equal, and three of these four columns are the kind of ratio that lands on
    the same number by coincidence.
    """
    defaults: dict[str, object] = {
        "net_profit": Decimal("1200"),
        "gross_profit": Decimal("2000"),
        "gross_loss": Decimal("-800"),
        "total_trades": 40,
        "long_trades": 25,
        "short_trades": 15,
        "win_rate": Decimal("0.55"),
        "payoff": Decimal("1.4"),
        "profit_factor": Decimal("2.5"),
        "expectancy": Decimal("30"),
        "max_drawdown_abs": Decimal("300"),
        "max_drawdown_pct": Decimal("0.03"),
        "max_dd_duration_days": 4,
        "sharpe": Decimal("1.9"),
        "sortino": Decimal("2.7"),
        "cagr": Decimal("0.18"),
        "equity_curve": [],
    }
    return BacktestMetrics(**(defaults | overrides))


@pytest.mark.parametrize(
    ("metric", "expected"),
    [
        (SelectionMetric.NET_PROFIT, Decimal("1200")),
        (SelectionMetric.PROFIT_FACTOR, Decimal("2.5")),
        (SelectionMetric.SHARPE, Decimal("1.9")),
        (SelectionMetric.EXPECTANCY, Decimal("30")),
    ],
)
def test_each_metric_reads_its_own_column(metric: SelectionMetric, expected: Decimal) -> None:
    """The ranking a fold was asked for is the ranking it gets.

    Worth its own test because the failure is silent in the worst way: a walk-forward asked to
    select on Sharpe but selecting on net profit still runs, still fills every fold, and still
    reports an out-of-sample number — one that answers a question nobody asked.
    """
    assert _METRIC_OF[metric](_metrics()) == expected


@pytest.mark.parametrize(
    "metric",
    [SelectionMetric.PROFIT_FACTOR, SelectionMetric.SHARPE, SelectionMetric.EXPECTANCY],
)
def test_an_undefined_metric_stays_undefined(metric: SelectionMetric) -> None:
    """Three of the four columns are nullable, and null has to survive the read.

    Sharpe over zero trades is undefined, not flat. If it arrived here as a zero, `choose` could
    never tell "no score" from "scored nothing" — and in a losing window a zero beats every
    point that traded and lost.
    """
    assert _METRIC_OF[metric](_metrics(**{metric.value: None})) is None


def test_every_selection_metric_is_readable() -> None:
    """The mapping covers the enum, checked over the enum rather than over a list.

    A metric added to `SelectionMetric` without a line in `_METRIC_OF` would pass the request
    validator, reach the worker, and die with a `KeyError` inside a job — minutes into an
    experiment, with the failure recorded against the walk-forward rather than the code.
    """
    assert set(_METRIC_OF) == set(SelectionMetric)


def test_a_run_that_has_not_finished_returns_null_and_never_zero() -> None:
    """A fold's missing number is an absence of measurement, not a measurement of nothing.

    Zero would place the fold in the median, count it against `folds_profitable`, and drag the
    compounded figure toward nothing — all while the run it came from was still queued.
    """
    assert _return_of(None, Decimal("10000")) is None
    assert _return_of(Backtest(), Decimal("10000")) is None


def test_a_return_is_a_fraction_of_the_capital_the_run_started_with() -> None:
    """Fractions, not currency, because that is what makes folds comparable and compoundable."""
    run = Backtest()
    run.metrics = _metrics(net_profit=Decimal("1200"))

    assert _return_of(run, Decimal("10000")) == Decimal("0.12")

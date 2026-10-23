"""The target ladder across a sweep entry's runs: one row per rung, medians first, the best beside.

No database: the runs are ORM objects built in memory, carrying only what the summary reads.
"""

from decimal import Decimal
from typing import Any

from tradeforge_api.targets import rungs_across
from tradeforge_db.models import Backtest, BacktestMetrics
from tradeforge_engine.excursion import LADDER


def _run(targets: dict[str, Any] | None) -> Backtest:
    run = Backtest()
    run.metrics = BacktestMetrics(targets=targets)
    return run


def _rung(net: str, expectancy: str, trades: int = 10) -> dict[str, Any]:
    return {
        "trades": trades,
        "hits": 4,
        "net_r": net,
        "expectancy_r": expectancy,
        "max_drawdown_r": "1",
    }


def test_every_rung_of_the_ladder_gets_a_row_lowest_first() -> None:
    rows = rungs_across([])
    assert [row.rung for row in rows] == [format(k.normalize(), "f") for k in LADDER]
    assert all(row.runs_scored == 0 and row.median_expectancy_r is None for row in rows)


def test_a_rung_is_summarised_by_its_median_and_its_best_beside_it() -> None:
    """Three runs at 2 R: expectancies 0.1, 0.3, -0.2 — the median is 0.1, two are positive, and
    the best by net R is named, because the best alone is the flattering answer."""
    runs = [
        (_run({"2": _rung("1", "0.1")}), "EURUSD · M15 · period=9"),
        (_run({"2": _rung("3", "0.3")}), "EURUSD · M15 · period=21"),
        (_run({"2": _rung("-2", "-0.2")}), "GBPUSD · M15 · period=9"),
    ]
    [two] = [row for row in rungs_across(runs) if row.rung == "2"]
    assert (two.runs_scored, two.runs_positive) == (3, 2)
    assert two.median_expectancy_r == Decimal("0.1")
    assert (two.best_label, two.best_net_r) == ("EURUSD · M15 · period=21", Decimal(3))


def test_a_run_that_could_not_answer_a_rung_is_left_out_of_it_not_counted_as_zero() -> None:
    """A null rung (some trade could not answer it) and a run with no ladder at all (recorded
    before it existed) both stay out of the rung, rather than pulling its median towards zero."""
    runs = [
        (_run({"2": _rung("1", "0.5"), "3": None}), "a"),
        (_run(None), "b"),
    ]
    rows = {row.rung: row for row in rungs_across(runs)}
    assert rows["2"].runs_scored == 1
    assert rows["3"].runs_scored == 0
    assert rows["3"].median_expectancy_r is None


def test_a_run_without_results_yet_is_left_out() -> None:
    run = Backtest()
    run.metrics = None
    assert rungs_across([(run, "queued")])[0].runs_scored == 0


def test_a_run_that_made_no_trades_says_nothing_about_any_target() -> None:
    runs = [
        (_run({"2": _rung("0", "0", trades=0)}), "empty"),
        (_run({"2": _rung("2", "0.2")}), "b"),
    ]
    [two] = [row for row in rungs_across(runs) if row.rung == "2"]
    assert two.runs_scored == 1
    assert two.median_expectancy_r == Decimal("0.2")


def test_breaking_even_is_not_positive_and_the_best_is_by_net_not_per_trade() -> None:
    """A run at exactly zero is not counted positive. And the best is the run that made the most,
    not the one with the best average: two trades at +1 R beat twenty at +0.1 R per trade only
    if the twenty did not make more in total — here they did."""
    runs = [
        (_run({"2": _rung("0", "0")}), "flat"),
        (_run({"2": _rung("2", "1", trades=2)}), "two good trades"),
        (_run({"2": _rung("3", "0.15", trades=20)}), "twenty small ones"),
    ]
    [two] = [row for row in rungs_across(runs) if row.rung == "2"]
    assert two.runs_positive == 2
    assert (two.best_label, two.best_net_r) == ("twenty small ones", Decimal(3))

"""A sweep's walk-forward answered by cutting its own runs (01/10) — ranking on the training cut,
reading the test cut, every number worked by hand."""

import datetime as dt
from dataclasses import replace
from decimal import Decimal
from typing import Any

from tradeforge_api.sweep_walkforward import windows
from tradeforge_api.sweep_walkforward_cut import (
    CutRule,
    CutRun,
    Excluded,
    choose_by_cut,
    fold_cut,
    fold_document,
    train_cut,
    uncovered_years,
)

CAPITAL = Decimal("10000")
FROM = dt.datetime(2020, 1, 1, tzinfo=dt.UTC)
TO = dt.datetime(2024, 1, 1, tzinfo=dt.UTC)
RULE = CutRule(top_n=1, floors={"H1": 5})


def healthy() -> dict[str, dict[str, str | None]]:
    return {
        str(year): {"equity_at_start": str(CAPITAL), "smallest_volume": "0.40"}
        for year in range(2020, 2024)
    }


def a_run(  # noqa: PLR0913 — keyword-only; each is one thing a test varies
    order: int,
    *,
    r: dict[int, str],
    trades: dict[int, int] | None,
    symbol: str = "EURUSD",
    timeframe: str = "H1",
    refusals: int = 0,
    behaviour: tuple[object, ...] | None = None,
) -> CutRun:
    """A run whose every trade left in the year it entered: `r` and `trades` by year."""
    return CutRun(
        run_id=f"run-{order}",
        group=("entry", timeframe, symbol),
        order=order,
        label=f"{symbol} · point {order}",
        behaviour=behaviour if behaviour is not None else (order,),
        r_by_years={str(year): {str(year): value} for year, value in r.items()},
        trades_by_years=None
        if trades is None
        else {str(year): {str(year): count} for year, count in trades.items()},
        sizing_by_years=healthy(),
        sizing_refusals=refusals,
        initial_capital=CAPITAL,
        date_from=FROM,
        date_to=TO,
    )


EVERY_YEAR = {2020: 5, 2021: 5, 2022: 5, 2023: 5}


def test_the_best_training_cut_is_chosen_not_the_best_run() -> None:
    """Over 2020-21 run 1 made 6 R and run 0 made 4; over the whole run 0 is the better (by its
    2023), which a cut of the training years must not see."""
    runs = [
        a_run(0, r={2020: "2", 2021: "2", 2023: "50"}, trades=EVERY_YEAR),
        a_run(1, r={2020: "3", 2021: "3", 2023: "-1"}, trades=EVERY_YEAR),
    ]

    chosen, excluded = choose_by_cut(runs, 2020, 2021, RULE)

    assert [(run.run_id, cut.net_r, cut.trades) for run, cut in chosen] == [
        ("run-1", Decimal("6"), 10)
    ]
    assert excluded == {}


def test_the_floor_reads_the_cuts_trades_not_the_runs() -> None:
    """Twenty trades in the run, four in 2020-21: under the H1 floor of five on the cut."""
    runs = [
        a_run(0, r={2020: "9", 2021: "9"}, trades={2020: 2, 2021: 2, 2022: 16}),
        a_run(1, r={2020: "1", 2021: "1"}, trades=EVERY_YEAR),
    ]

    chosen, excluded = choose_by_cut(runs, 2020, 2021, RULE)

    assert [run.run_id for run, _cut in chosen] == ["run-1"]
    assert excluded == {Excluded.UNDER_FLOOR.value: 1}


def test_runs_without_counts_and_runs_the_guard_refuses_are_left_out_by_reason() -> None:
    runs = [
        a_run(0, r={2020: "9"}, trades=None),
        a_run(1, r={2020: "9"}, trades=EVERY_YEAR, refusals=1),
        a_run(2, r={2020: "1"}, trades=EVERY_YEAR),
    ]

    chosen, excluded = choose_by_cut(runs, 2020, 2021, RULE)

    assert [run.run_id for run, _cut in chosen] == ["run-2"]
    assert excluded == {"no_counts": 1, "refused_cut": 1}
    assert train_cut(runs[0], 2020, 2021, RULE) is Excluded.NO_COUNTS


def test_a_clone_of_the_whole_run_gives_its_place_to_the_next() -> None:
    """Runs 0 and 1 did the same thing (`behaviour`); with top 2 the second place goes to run 2."""
    same = ("same",)
    runs = [
        a_run(0, r={2020: "5"}, trades=EVERY_YEAR, behaviour=same),
        a_run(1, r={2020: "5"}, trades=EVERY_YEAR, behaviour=same),
        a_run(2, r={2020: "1"}, trades=EVERY_YEAR),
    ]
    rule = CutRule(top_n=2, floors={"H1": 5})

    distinct, _ = choose_by_cut(runs, 2020, 2021, rule)
    every, _ = choose_by_cut(runs, 2020, 2021, CutRule(top_n=2, floors={"H1": 5}, distinct=False))

    assert [run.run_id for run, _cut in distinct] == ["run-0", "run-2"]
    assert [run.run_id for run, _cut in every] == ["run-0", "run-1"]


def test_each_market_and_chart_chooses_on_its_own() -> None:
    runs = [
        a_run(0, r={2020: "5"}, trades=EVERY_YEAR),
        a_run(1, r={2020: "1"}, trades=EVERY_YEAR, symbol="GBPUSD"),
        a_run(2, r={2020: "9"}, trades=EVERY_YEAR, timeframe="H4"),
    ]

    chosen, _ = choose_by_cut(runs, 2020, 2021, CutRule(top_n=1, floors={"H1": 5, "H4": 5}))

    assert [run.run_id for run, _cut in chosen] == ["run-0", "run-1", "run-2"]


def test_the_share_of_positive_years_is_the_cuts() -> None:
    """Run 0: 2020 +4, 2021 -1 — half its years; run 1 both positive."""
    runs = [
        a_run(0, r={2020: "4", 2021: "-1", 2022: "3"}, trades=EVERY_YEAR),
        a_run(1, r={2020: "1", 2021: "1", 2022: "-9"}, trades=EVERY_YEAR),
    ]
    rule = CutRule(top_n=1, floors={"H1": 5}, min_positive_year_share=Decimal("0.75"))

    chosen, excluded = choose_by_cut(runs, 2020, 2021, rule)

    assert [run.run_id for run, _cut in chosen] == ["run-1"]
    assert excluded == {"positive_years": 1}


def test_a_fold_reads_the_chosen_on_its_test_years_and_a_test_with_no_trade_apart() -> None:
    """Top 2 of three over 2020-21; tested on 2022: run 0 made 3 R in 5 trades, run 1 none."""
    runs = [
        a_run(0, r={2020: "2", 2021: "2", 2022: "3"}, trades=EVERY_YEAR),
        a_run(1, r={2020: "5", 2021: "5"}, trades={2020: 5, 2021: 5}),
        a_run(2, r={2020: "1"}, trades=EVERY_YEAR),
    ]
    window = windows(start_year=2020, train_years=2, test_years=1, folds=2, anchored=True)[0]

    found = fold_cut(runs, window, CutRule(top_n=2, floors={"H1": 5}))

    assert found.candidates == 3
    assert [(pick.run.run_id, pick.test and pick.test.trades) for pick in found.picks] == [
        ("run-0", 5),
        ("run-1", 0),
    ]
    (group,) = found.groups
    assert (group.points, group.no_trades_out, group.no_cut_out) == (2, 1, 0)
    assert group.in_sample_median_r == Decimal("7")  # median of 4 and 10
    assert group.out_of_sample_median_r == Decimal("3")
    assert group.out_of_sample_positive == Decimal(1)
    assert group.chosen == ["EURUSD · point 0", "EURUSD · point 1"]


def test_a_test_cut_the_guard_refuses_is_counted_apart() -> None:
    """Fine to start in 2020, but a run started in 2022 at a quarter of what this held then would
    size 2022's 0.04 at 0.01 — under two steps."""
    run = a_run(0, r={2020: "2", 2021: "2", 2022: "3"}, trades=EVERY_YEAR)
    sizing = healthy()
    sizing["2022"] = {"equity_at_start": "40000", "smallest_volume": "0.04"}
    run = replace(run, sizing_by_years=sizing)
    window = windows(start_year=2020, train_years=2, test_years=1, folds=2, anchored=True)[0]

    found = fold_cut([run], window, RULE)

    (pick,) = found.picks
    assert pick.test is None
    assert "under 2 steps" in (pick.test_refused or "")
    (group,) = found.groups
    assert (group.no_cut_out, group.out_of_sample_median_r) == (1, None)
    kept: dict[str, Any] = fold_document(found)
    assert kept["chosen"][0]["test_r"] is None
    assert kept["groups"][0]["no_cut_out"] == 1


def test_the_document_keeps_decimals_as_strings() -> None:
    runs = [a_run(0, r={2020: "2.5", 2021: "1", 2022: "-1"}, trades=EVERY_YEAR)]
    window = windows(start_year=2020, train_years=2, test_years=1, folds=2, anchored=True)[0]

    kept = fold_document(fold_cut(runs, window, RULE))

    assert kept["excluded"] == {}
    assert kept["chosen"][0] == {
        "run_id": "run-0",
        "entry_id": "entry",
        "timeframe": "H1",
        "symbol": "EURUSD",
        "label": "EURUSD · point 0",
        "train_r": "3.5",
        "train_trades": 10,
        "test_r": "-1",
        "test_trades": 5,
        "test_refused": None,
    }
    assert kept["groups"][0]["out_of_sample_positive"] == "0"


def test_years_outside_the_parent_or_only_partly_in_it_are_named() -> None:
    planned = windows(start_year=2019, train_years=2, test_years=1, folds=3, anchored=False)
    # Folds need 2019..2023; the parent ran from March 2020 to 2023-01-01.
    outside, partial = uncovered_years(
        planned, dt.datetime(2020, 3, 1, tzinfo=dt.UTC), dt.datetime(2023, 1, 1, tzinfo=dt.UTC)
    )

    assert (outside, partial) == ([2019, 2023], [2020])
    whole = dt.datetime(2019, 1, 1, tzinfo=dt.UTC), dt.datetime(2024, 1, 1, tzinfo=dt.UTC)
    assert uncovered_years(planned, *whole) == ([], [])

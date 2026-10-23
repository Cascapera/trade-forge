"""The dashboard's arithmetic, on hand-worked numbers.

Every expected value below is computed in the comment beside it, not read off the code.
"""

import datetime as dt
import uuid
from decimal import Decimal

from tradeforge_api.sweep_dashboard import (
    DashboardRun,
    DashboardSweepRow,
    RunResult,
    by_entry,
    by_symbol,
    by_timeframe,
    distinct,
    left_out,
    median,
    per_sweep,
    ratios,
    summarise,
    totals,
)
from tradeforge_db.models import BacktestStatus

CAPITAL = Decimal(10000)
S1, S2, S3 = (str(uuid.UUID(int=n)) for n in (1, 2, 3))


def result(  # noqa: PLR0913 — one keyword per measured field a test varies
    net: int | str,
    *,
    trades: int = 4,
    win_rate: str = "0.5",
    profit_factor: str | None = "1.5",
    expectancy: str | None = "25",
    drawdown: str = "0.1",
) -> RunResult:
    return RunResult(
        net_profit=Decimal(net),
        total_trades=trades,
        win_rate=Decimal(win_rate),
        profit_factor=None if profit_factor is None else Decimal(profit_factor),
        expectancy=None if expectancy is None else Decimal(expectancy),
        max_drawdown_pct=Decimal(drawdown),
    )


def run(  # noqa: PLR0913 — one keyword per label a run is grouped by
    outcome: RunResult | None = None,
    *,
    status: BacktestStatus | None = None,
    sweep: str = S1,
    entry: str = "e1",
    name: str | None = "alpha",
    symbol: str = "EURUSD",
    timeframe: str = "M15",
    capital: Decimal = CAPITAL,
    measurement: object = None,
) -> DashboardRun:
    return DashboardRun(
        sweep_id=sweep,
        entry_id=entry,
        entry_name=name,
        symbol=symbol,
        timeframe=timeframe,
        status=status or (BacktestStatus.DONE if outcome is not None else BacktestStatus.QUEUED),
        initial_capital=capital,
        # Unique unless a test says two runs are the same measurement.
        measurement=measurement if measurement is not None else object(),
        result=outcome,
    )


class TestMedian:
    def test_the_middle_of_an_odd_count(self) -> None:
        assert median([Decimal(3), Decimal(1), Decimal(2)]) == Decimal(2)

    def test_the_mean_of_the_two_middles_of_an_even_count(self) -> None:
        # (0.01 + 0.03) / 2 = 0.02 — and unsorted input, so a median that forgot to sort fails.
        assert median([Decimal("0.03"), Decimal("0.01")]) == Decimal("0.02")

    def test_nothing_has_no_median_rather_than_zero(self) -> None:
        assert median([]) is None


class TestDistinct:
    def test_a_copy_counts_once_and_the_finished_copy_is_the_one_kept(self) -> None:
        # Three launches of measurement "m": queued, done (+300), failed. One of "n", done.
        runs = [
            run(measurement="m"),
            run(result(300), measurement="m"),
            run(status=BacktestStatus.FAILED, measurement="m"),
            run(result(-100), measurement="n"),
        ]

        kept = distinct(runs)

        assert len(kept) == 2
        assert [one.result.net_profit if one.result else None for one in kept] == [
            Decimal(300),
            Decimal(-100),
        ]

    def test_the_first_finished_copy_wins_over_a_later_one(self) -> None:
        first, second = run(result(100), measurement="m"), run(result(100), measurement="m")

        assert distinct([first, second]) == [first]

    def test_unrelated_runs_are_all_kept_in_order(self) -> None:
        runs = [run(result(n)) for n in (3, 1, 2)]

        assert distinct(runs) == runs


class TestSummarise:
    def test_a_group_counts_what_finished_and_splits_it_by_sign(self) -> None:
        # Returns: 300/10000 = 0.03, -100/10000 = -0.01, 0/10000 = 0. One queued, one failed.
        # median(-0.01, 0, 0.03) = 0; mean = 0.02 / 3; drawdowns 0.1, 0.3, 0.2 → median 0.2.
        runs = [
            run(result(300, drawdown="0.1")),
            run(result(-100, drawdown="0.3")),
            run(result(0, drawdown="0.2")),
            run(),
            run(status=BacktestStatus.FAILED),
        ]

        got = summarise("all", "All sweeps", runs)

        assert (got.runs, got.finished, got.failed) == (5, 3, 1)
        assert (got.winners, got.losers, got.flat) == (1, 1, 1)
        assert got.median_return == Decimal(0)
        assert got.mean_return == Decimal("0.02") / 3
        assert (got.best_return, got.worst_return) == (Decimal("0.03"), Decimal("-0.01"))
        assert (got.median_drawdown, got.worst_drawdown) == (Decimal("0.2"), Decimal("0.3"))

    def test_a_return_is_over_the_runs_own_capital(self) -> None:
        # 100 on 1 000 is 10 %; 100 on 10 000 is 1 %. One shared capital would make them equal.
        got = summarise("all", None, [run(result(100), capital=Decimal(1000)), run(result(100))])

        assert (got.best_return, got.worst_return) == (Decimal("0.1"), Decimal("0.01"))

    def test_nothing_finished_is_null_not_zero(self) -> None:
        got = summarise("all", None, [run(), run(status=BacktestStatus.RUNNING)])

        assert (got.runs, got.finished, got.winners, got.losers, got.flat) == (2, 0, 0, 0, 0)
        assert got.median_return is None
        assert got.mean_return is None
        assert got.best_return is None
        assert got.worst_return is None
        assert got.median_drawdown is None
        assert got.worst_drawdown is None


class TestRatios:
    def test_each_ratio_is_taken_over_the_runs_where_it_exists(self) -> None:
        runs = [
            # Traded, no losing trade: a win rate, no profit factor.
            run(result(200, win_rate="1", profit_factor=None, expectancy="50")),
            # Traded and lost: all three.
            run(result(-100, win_rate="0.25", profit_factor="0.5", expectancy="-10")),
            # Never traded: its win rate of 0 is not a measurement and must not drag the median.
            run(result(0, trades=0, win_rate="0", profit_factor=None, expectancy=None)),
            run(),  # not finished
        ]

        win_rate, profit_factor, expectancy = ratios(runs)

        # median(1, 0.25) = 0.625 over two runs — with the zero-trade run it would be 0.25.
        assert (win_rate.median, win_rate.runs) == (Decimal("0.625"), 2)
        assert (profit_factor.median, profit_factor.runs) == (Decimal("0.5"), 1)
        # Per trade, over each run's capital: 50/10000 = 0.005, -10/10000 = -0.001 → 0.002.
        assert (expectancy.median, expectancy.runs) == (Decimal("0.002"), 2)

    def test_no_finished_run_gives_no_ratio(self) -> None:
        for ratio in ratios([run()]):
            assert (ratio.median, ratio.runs) == (None, 0)


class TestGroups:
    def test_entries_are_ranked_by_their_median_never_by_their_best(self) -> None:
        # alpha: returns 0.5, 0.01, 0.00 → median 0.01, best 0.5.
        # beta:  returns 0.03, 0.02      → median 0.025, best 0.03.
        # gamma: nothing finished        → no median, last.
        runs = [
            run(result(5000), entry="a", name="alpha"),
            run(result(100), entry="a", name="alpha"),
            run(result(0), entry="a", name="alpha"),
            run(result(300), entry="b", name="beta"),
            run(result(200), entry="b", name="beta"),
            run(entry="c", name="gamma"),
        ]

        got = by_entry(runs)

        assert [one.label for one in got] == ["beta", "alpha", "gamma"]
        assert [one.median_return for one in got] == [Decimal("0.025"), Decimal("0.01"), None]
        assert [one.runs for one in got] == [2, 3, 1]

    def test_a_tie_is_broken_by_the_key_so_the_order_is_reproducible(self) -> None:
        runs = [run(result(100), entry="z"), run(result(100), entry="m")]

        assert [one.key for one in by_entry(runs)] == ["m", "z"]
        assert [one.key for one in by_entry(list(reversed(runs)))] == ["m", "z"]

    def test_a_negative_median_ranks_below_zero(self) -> None:
        # A sort on the magnitude, or on the median ascending, would put -0.05 first.
        runs = [run(result(-500), entry="down"), run(result(0), entry="flat")]

        assert [one.key for one in by_entry(runs)] == ["flat", "down"]

    def test_a_removed_entry_keeps_its_runs_and_loses_only_its_name(self) -> None:
        (got,) = by_entry([run(result(100), entry="gone", name=None)])

        assert (got.key, got.label, got.runs) == ("gone", None, 1)

    def test_markets_and_charts_are_grouped_across_entries(self) -> None:
        runs = [
            run(result(100), entry="a", symbol="EURUSD", timeframe="H4"),
            run(result(-100), entry="b", symbol="EURUSD", timeframe="M15"),
            run(result(300), entry="b", symbol="AUDUSD", timeframe="H4"),
        ]

        symbols = {one.key: (one.label, one.runs, one.winners) for one in by_symbol(runs)}
        charts = {one.key: (one.label, one.runs, one.winners) for one in by_timeframe(runs)}

        assert symbols == {"EURUSD": ("EURUSD", 2, 1), "AUDUSD": ("AUDUSD", 1, 1)}
        assert charts == {"H4": ("H4", 2, 2), "M15": ("M15", 1, 0)}


LAUNCH = dt.datetime(2026, 9, 15, 22, tzinfo=dt.UTC)


def sweep_row(
    sweep_id: str, *names: str | None, skipped: tuple[tuple[str, str], ...] = ()
) -> DashboardSweepRow:
    return DashboardSweepRow(
        sweep_id=sweep_id, created_at=LAUNCH, entry_names=list(names) or ["alpha"], skipped=skipped
    )


class TestTotals:
    def test_the_headline_counts(self) -> None:
        sweeps = [sweep_row(S1), sweep_row(S2)]
        runs = [
            run(result(100, trades=7), entry="a", symbol="EURUSD", timeframe="M15"),
            run(result(0, trades=0), entry="a", symbol="AUDUSD", timeframe="H4"),
            run(entry="b", symbol="EURUSD", timeframe="H4"),
            run(status=BacktestStatus.RUNNING, entry="b"),
            run(status=BacktestStatus.FAILED, entry="b"),
            run(status=BacktestStatus.FAILED, entry="b"),
        ]

        got = totals(sweeps, runs)

        assert (got.sweeps, got.entries) == (2, 2)
        assert got.symbols == ["AUDUSD", "EURUSD"]
        assert got.timeframes == ["H4", "M15"]
        # Four statuses, four different counts, so no two can be swapped unnoticed.
        assert got.runs.model_dump() == {
            "total": 6,
            "done": 2,
            "running": 1,
            "queued": 1,
            "failed": 2,
        }
        assert (got.trades, got.runs_without_trades) == (7, 1)
        assert got.measurements == 6

    def test_launches_count_every_copy_and_results_count_each_measurement_once(self) -> None:
        # The same finished measurement launched twice, 7 trades each: two runs, one measurement,
        # seven trades — not fourteen.
        runs = [
            run(result(100, trades=7), measurement="m"),
            run(result(100, trades=7), measurement="m"),
            run(result(0, trades=0), measurement="z"),
            run(result(0, trades=0), measurement="z"),
        ]

        got = totals([sweep_row(S1), sweep_row(S2)], runs)

        assert (got.runs.total, got.runs.done, got.measurements) == (4, 4, 2)
        assert (got.trades, got.runs_without_trades) == (7, 1)


class TestPerSweep:
    def test_each_sweep_keeps_its_own_runs_and_its_place_in_the_timeline(self) -> None:
        # S2 is listed first on purpose: the order is the caller's (launch order), not the ids'.
        sweeps = [sweep_row(S2, "beta", None), sweep_row(S1), sweep_row(S3)]
        runs = [
            run(result(100), sweep=S1),
            run(result(300), sweep=S1),
            run(result(-200), sweep=S2),
            run(sweep=S2),
        ]

        got = per_sweep(sweeps, runs)

        assert [str(one.id) for one in got] == [S2, S1, S3]
        assert [(one.runs, one.finished, one.winners) for one in got] == [
            (2, 1, 0),
            (2, 2, 2),
            (0, 0, 0),
        ]
        # S1: median(0.01, 0.03) = 0.02; S2: -0.02; S3 ran nothing.
        assert [one.median_return for one in got] == [Decimal("-0.02"), Decimal("0.02"), None]
        assert got[0].entry_names == ["beta", None]
        assert got[0].created_at == LAUNCH


class TestLeftOut:
    """His call (22/09): the dashboard names the pairs its sweeps skipped. They have no runs, so
    no table can show them, and the market and chart breakdowns would otherwise read as the whole
    space that was asked for."""

    def test_each_pair_is_named_once_with_how_many_sweeps_skipped_it(self) -> None:
        sweeps = [
            sweep_row(S1, skipped=(("GBPUSD", "H1"), ("EURUSD", "M15"))),
            sweep_row(S2, skipped=(("GBPUSD", "H1"),)),
            sweep_row(S3),
        ]

        got = [(one.symbol, one.timeframe, one.sweeps) for one in left_out(sweeps)]

        # By symbol, then chart — the same order the totals list their markets in.
        assert got == [("EURUSD", "M15", 1), ("GBPUSD", "H1", 2)]

    def test_a_pair_named_twice_by_one_sweep_is_one_sweep(self) -> None:
        # The count is how often the period asked and got nothing, not how often it was written.
        sweeps = [sweep_row(S1, skipped=(("GBPUSD", "H1"), ("GBPUSD", "H1")))]

        [only] = left_out(sweeps)

        assert only.sweeps == 1

    def test_nothing_skipped_is_an_empty_list(self) -> None:
        assert left_out([sweep_row(S1), sweep_row(S2)]) == []

    def test_the_totals_carry_it_and_each_sweep_counts_its_own(self) -> None:
        sweeps = [
            sweep_row(S1, skipped=(("GBPUSD", "H1"), ("EURUSD", "M15"), ("GBPUSD", "H1"))),
            sweep_row(S2),
        ]

        assert [one.symbol for one in totals(sweeps, []).left_out] == ["EURUSD", "GBPUSD"]
        assert [one.left_out for one in per_sweep(sweeps, [])] == [2, 0]

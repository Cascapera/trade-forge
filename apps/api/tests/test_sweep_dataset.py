"""A sweep's dataset, built from in-memory runs — no database, no HTTP.

The file is read back with `csv.DictReader`, the way pandas or an AI's script would read it,
rather than by comparing strings: what matters is what a reader parses, not how the bytes happen
to be laid out.
"""

import csv
import datetime as dt
import io
import uuid
from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import Any

from tradeforge_api.sweep_dataset import (
    OMITTED,
    PARAM_PREFIX,
    DatasetRun,
    cell,
    columns_for,
    to_csv,
)
from tradeforge_db.models import Backtest, BacktestMetrics, BacktestStatus
from tradeforge_engine.excursion import LADDER

START = dt.datetime(2024, 1, 1, tzinfo=dt.UTC)


def a_run(  # noqa: PLR0913 — keyword-only; one knob per fact a row is read from
    *,
    values: Mapping[str, Any] | None = None,
    entry_name: str | None = "9.1",
    net_profit: str | None = "250",
    total_trades: int = 12,
    win_rate: str = "0.50000000",
    status: BacktestStatus = BacktestStatus.DONE,
    error: str | None = None,
) -> DatasetRun:
    """A run carrying what its row reads. `net_profit=None` is a run with no metrics yet."""
    run = Backtest(
        id=uuid.UUID(int=1),
        strategy_id=uuid.UUID(int=2),
        timeframe="M15",
        date_from=START,
        date_to=START + dt.timedelta(days=30),
        initial_capital=Decimal("10000.00000000"),
        cost_model={"type": "spread", "spread_points": 8},
        status=status,
        error=error,
        engine_version="0.1.0",
    )
    run.metrics = (
        None
        if net_profit is None
        else BacktestMetrics(
            net_profit=Decimal(net_profit),
            total_trades=total_trades,
            long_trades=total_trades,
            short_trades=0,
            win_rate=Decimal(win_rate),
            payoff=None,
            profit_factor=Decimal("1.25000000"),
            expectancy=Decimal("20.83333333"),
            max_drawdown_pct=Decimal("0.03000000"),
            sharpe=None,
            sortino=None,
            avg_trade_duration=dt.timedelta(hours=2),
        )
    )
    return DatasetRun(
        sweep_id=str(uuid.UUID(int=9)),
        run=run,
        entry_id=str(uuid.UUID(int=3)),
        entry_name=entry_name,
        strategy_version=1,
        symbol="EURUSD",
        asset_class="forex",
        values=values if values is not None else {"timeframe": "M15"},
    )


def an_idle_run() -> DatasetRun:
    """A finished run that closed no trade, carrying the metrics the engine writes for one.

    ⚠️ Not `a_run(total_trades=0)` alone: that keeps the expectancy and duration of a run with
    trades, and a guard against their being null would then go untested — the null a real idle
    run carries is exactly what those guards stand between and a `TypeError`.
    """
    row = a_run(net_profit="0", total_trades=0, win_rate="0E-8")
    metrics = row.run.metrics
    assert metrics is not None
    metrics.profit_factor = None
    metrics.expectancy = None
    metrics.avg_trade_duration = None
    return row


def table(runs: Sequence[DatasetRun]) -> list[dict[str, str]]:
    return list(csv.DictReader(io.StringIO(to_csv(runs))))


def header(runs: Sequence[DatasetRun]) -> list[str]:
    return next(csv.reader(io.StringIO(to_csv(runs))))


class TestTheDictionary:
    def test_every_column_is_described_with_a_role_and_a_unit(self) -> None:
        # The dictionary is the file's legend. A column with a blank description is one an AI
        # has to guess at, which is the failure the dictionary exists to prevent.
        runs = [a_run(values={"timeframe": "M15", "setup.params.period": 9})]
        columns = columns_for(runs)

        assert [column.name for column in columns] == header(runs)
        assert len({column.name for column in columns}) == len(columns)
        for column in columns:
            assert column.role in {"identity", "choice", "condition", "outcome"}, column.name
            assert column.unit.strip(), column.name
            assert column.description.strip(), column.name

    def test_the_risk_ratios_say_they_are_per_trade_and_not_annualised(self) -> None:
        # ⚠️ The engine computes both over per-trade returns. Read as annual figures they are on
        # the wrong scale, and "a Sharpe above 1 is good" becomes a false rule for this column.
        units = {column.name: column.unit for column in columns_for([a_run()])}

        assert "not annualised" in units["sharpe"]
        assert "not annualised" in units["sortino"]

    def test_the_drawdown_duration_is_left_out_and_the_dictionary_says_why(self) -> None:
        # ⚠️ Truncated to whole days, an intraday drawdown reads 0. In a dataset that zero is a
        # claim — "it never lasted" — and a model would learn from it.
        names = header([a_run()])

        assert "max_dd_duration_days" not in names
        assert "cagr" not in names
        omitted = " ".join(name for name, _ in OMITTED)
        assert "max_dd_duration_days" in omitted
        assert "cagr" in omitted


class TestTheGridColumns:
    def test_a_path_one_entry_does_not_sweep_is_empty_not_zero(self) -> None:
        # Entries sweep different axes. A run whose entry has no `period` did not run with
        # period 0, and a zero here would be a value nobody chose.
        rows = table(
            [
                a_run(values={"timeframe": "M15", "setup.params.period": 9}),
                a_run(values={"timeframe": "M15"}),
            ]
        )

        assert rows[0][f"{PARAM_PREFIX}setup.params.period"] == "9"
        assert rows[1][f"{PARAM_PREFIX}setup.params.period"] == ""

    def test_a_chosen_null_is_not_an_unswept_path(self) -> None:
        # ⚠️ `breakeven_at_r: null` is break-even switched off, a value the grid chose. The run
        # beside it does not sweep the path and ran with its document's own break-even. Written
        # alike, a model would learn that "off" and "at 2R" are the same run.
        rows = table(
            [
                a_run(values={"timeframe": "M15", "setup.params.breakeven_at_r": None}),
                a_run(values={"timeframe": "M15"}),
            ]
        )

        assert rows[0][f"{PARAM_PREFIX}setup.params.breakeven_at_r"] == "null"
        assert rows[1][f"{PARAM_PREFIX}setup.params.breakeven_at_r"] == ""

    def test_the_timeframe_is_not_repeated_as_a_parameter(self) -> None:
        names = header([a_run(values={"timeframe": "H1", "setup.params.period": 9})])

        assert "timeframe" in names
        assert f"{PARAM_PREFIX}timeframe" not in names

    def test_one_sweep_always_yields_one_header(self) -> None:
        # Sorted rather than in the order the rows happen to arrive: two downloads of the same
        # sweep must have the same columns in the same place, or joining them is a bug waiting.
        names = header(
            [
                a_run(values={"timeframe": "M15", "setup.params.period": 9}),
                a_run(values={"timeframe": "M15", "risk.params.percent": 1.0}),
            ]
        )

        grid = [name for name in names if name.startswith(PARAM_PREFIX)]
        assert grid == [f"{PARAM_PREFIX}risk.params.percent", f"{PARAM_PREFIX}setup.params.period"]


class TestTheOutcomes:
    def test_the_return_is_net_profit_over_the_starting_capital(self) -> None:
        (row,) = table([a_run(net_profit="250")])

        assert Decimal(row["return"]) == Decimal("0.025")

    def test_the_expectancy_is_a_fraction_of_the_capital_not_money(self) -> None:
        # In money it scales with the capital, the reason every other money column is left out.
        (row,) = table([a_run()])

        assert "expectancy" not in row
        assert Decimal(row["expectancy_per_trade"]) == Decimal("20.83333333") / Decimal("10000")

    def test_an_unfinished_run_keeps_its_row_with_every_outcome_empty(self) -> None:
        (row,) = table([a_run(net_profit=None, status=BacktestStatus.QUEUED)])

        assert row["status"] == "queued"
        for name in ("return", "total_trades", "win_rate", "avg_trade_duration_seconds"):
            assert row[name] == "", name

    def test_undefined_is_empty_and_zero_is_zero(self) -> None:
        # ⚠️ The pair that separates the two claims. A cell writer that blanked every falsy value
        # would pass a test of `payoff` alone and erase the measured zero beside it.
        (row,) = table([a_run()])

        assert row["payoff"] == ""
        assert row["short_trades"] == "0"

    def test_a_run_that_closed_no_trade_has_no_rates_and_still_writes_its_row(self) -> None:
        # ⚠️ The engine stores win_rate 0 — the column cannot be null — and leaves expectancy and
        # duration null. The first would read as a measured 0%; the other two, unguarded, would
        # raise and take the whole sweep's download down with one idle run.
        (idle,) = table([an_idle_run()])

        assert idle["total_trades"] == "0"
        assert idle["win_rate"] == ""
        assert idle["expectancy_per_trade"] == ""
        assert idle["avg_trade_duration_seconds"] == ""
        assert Decimal(idle["return"]) == 0

    def test_trades_that_all_lost_are_a_real_zero_win_rate(self) -> None:
        # The other side of the guard above: twelve trades and no winner is a measurement.
        (busy,) = table([a_run(total_trades=12, win_rate="0E-8")])

        assert busy["win_rate"] == "0.00000000"

    def test_a_failed_run_keeps_its_row_and_says_why(self) -> None:
        (row,) = table(
            [a_run(net_profit=None, status=BacktestStatus.FAILED, error="no candles in window")]
        )

        assert row["status"] == "failed"
        assert row["error"] == "no candles in window"

    def test_the_average_duration_is_in_whole_seconds(self) -> None:
        (row,) = table([a_run()])

        assert row["avg_trade_duration_seconds"] == "7200"


class TestTheCells:
    def test_a_removed_entry_leaves_its_name_empty(self) -> None:
        (row,) = table([a_run(entry_name=None)])

        assert row["entry_name"] == ""

    def test_a_name_with_a_comma_and_quotes_survives_the_round_trip(self) -> None:
        # Entry names are free text a person typed, and a comma is the separator.
        name = 'zeta, "com filtro"'

        (row,) = table([a_run(entry_name=name)])

        assert row["entry_name"] == name

    def test_a_timestamp_is_written_in_utc_whatever_zone_it_arrived_in(self) -> None:
        # ⚠️ The dictionary's unit says UTC. What a timestamp arrives in follows the database
        # session's timezone, which nothing in this project sets — so the file converts rather
        # than trusting that the server happens to run in UTC.
        three_hours_behind = dt.timezone(dt.timedelta(hours=-3))

        written = cell(dt.datetime(2024, 1, 1, 0, 0, tzinfo=three_hours_behind))

        assert written == "2024-01-01T03:00:00+00:00"

    def test_values_are_written_as_the_exact_text_a_reader_should_parse(self) -> None:
        assert cell(Decimal("0.10000000")) == "0.10000000"
        # A stored zero and a tiny value come back from NUMERIC in scientific notation.
        assert cell(Decimal("0E-8")) == "0.00000000"
        assert cell(Decimal("1.2E-7")) == "0.00000012"
        assert cell(True) == "true"
        assert cell(False) == "false"
        assert cell(0) == "0"
        assert cell(2.0) == "2.0"
        assert cell(START) == "2024-01-01T00:00:00+00:00"
        assert cell(BacktestStatus.DONE) == "done"
        # Keys sorted and no spaces, so one cost model is one string however it was written.
        assert cell({"type": "spread", "spread_points": 8}) == '{"spread_points":8,"type":"spread"}'


def _rows(runs: Sequence[DatasetRun]) -> list[dict[str, str]]:
    return list(csv.DictReader(io.StringIO(to_csv(runs))))


class TestTheTargetLadder:
    """Every rung of the engine's ladder, as columns — what the ML plan reads to choose a target
    without a run per target (24/09)."""

    def test_every_rung_has_its_five_columns_in_ladder_order(self) -> None:
        names = [column.name for column in columns_for([a_run()])]
        ladder = [name for name in names if name.startswith("target_")]

        rungs = [format(rung.normalize(), "f") for rung in LADDER]
        fields = ["net_r", "expectancy_r", "hits", "trades", "max_drawdown_r"]
        assert ladder == [f"target_{rung}r_{field}" for rung in rungs for field in fields]
        assert all(
            column.role == "outcome" for column in columns_for([a_run()]) if column.name in ladder
        )

    def test_a_scored_rung_is_read_as_numbers(self) -> None:
        run = a_run()
        assert run.run.metrics is not None
        run.run.metrics.targets = {
            "2": {
                "trades": 12,
                "hits": 4,
                "net_r": "1.50000000000000000000",
                "expectancy_r": "0.125",
                "max_drawdown_r": "3",
            },
            "3": None,
        }

        (row,) = _rows([run])

        assert row["target_2r_net_r"] == "1.50000000000000000000"
        assert Decimal(row["target_2r_expectancy_r"]) == Decimal("0.125")
        assert (row["target_2r_hits"], row["target_2r_trades"]) == ("4", "12")
        assert row["target_2r_max_drawdown_r"] == "3"

    def test_a_rung_that_cannot_be_scored_is_empty_never_zero(self) -> None:
        """`null` in the document is a rung some trade could not answer — a target of its own closer
        than this one, above all. Written as 0 it would read as a target that broke even."""
        scored = a_run()
        assert scored.run.metrics is not None
        scored.run.metrics.targets = {"3": None}
        unscored = a_run(net_profit=None, status=BacktestStatus.QUEUED)
        legacy = a_run()  # a run from before the ladder existed

        rows = _rows([scored, unscored, legacy])

        assert {row["target_3r_net_r"] for row in rows} == {""}
        assert {row["target_0.5r_hits"] for row in rows} == {""}

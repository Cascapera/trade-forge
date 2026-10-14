"""The grid's report: each entry under every stop and target, and the rules a model must beat."""

import datetime as dt
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from tradeforge_ml.grid import (
    EMPTY,
    GridRows,
    evaluate,
    grid_rows,
    outcome,
    summarise,
    taken,
)
from tradeforge_ml.grid_report import write_grid
from tradeforge_ml.model import Floor, Market, Split
from tradeforge_ml.replay_export import GRID_SCHEMA

VALIDATION = Split(dt.datetime(2023, 1, 1, tzinfo=dt.UTC), dt.datetime(2024, 1, 1, tzinfo=dt.UTC))
HOUR = dt.timedelta(hours=1)


def entry(  # noqa: PLR0913 — one per thing a test varies about an entry
    symbol: str,
    at: dt.datetime,
    r: Mapping[float, float],
    *,
    reasons: Mapping[float, str] | None = None,
    mfe: Mapping[float, float] | None = None,
    costs: float = 0.0,
    lasts: dt.timedelta = HOUR,
) -> list[dict[str, Any]]:
    """A long at 100 whose stop `k` sits `k` below it, ending at `r[k]` under each stop."""
    rows = []
    for multiple, result in r.items():
        reason = (reasons or {}).get(multiple, "sl" if result < 0 else "time")
        trade = reason in {"sl", "time"}
        rows.append(
            {
                "entry_id": "e",
                "symbol": symbol,
                "timeframe": "H1",
                "side": "long",
                "entry_time": at,
                "stop_multiple": multiple,
                "entry_price": 100.0,
                "stop_loss": 100.0 - multiple,
                "exit_time": at + lasts if trade else None,
                "exit_price": 100.0 + result * multiple + costs if trade else None,
                "exit_reason": reason,
                "r_multiple": result if trade else None,
                "mfe_r": (mfe or {}).get(multiple, max(result, 0.0)) if trade else None,
                "mae_r": None,
                "gross_pnl": None,
                "costs": costs if trade else None,
                "swap": 0.0 if trade else None,
                "net_pnl": None,
                "volume": 1.0 if trade else None,
            }
        )
    return rows


def table(*entries: list[dict[str, Any]]) -> pa.Table:
    return pa.Table.from_pylist([row for rows in entries for row in rows], schema=GRID_SCHEMA)


def at(year: int, hour: int = 0) -> dt.datetime:
    return dt.datetime(year, 3, 1, tzinfo=dt.UTC) + hour * HOUR


class TestTheFile:
    def test_one_row_per_entry_and_stop_read_as_a_matrix(self) -> None:
        rows = grid_rows(table(entry("A", at(2021), {1.0: -1.0, 2.0: 0.5}, costs=0.1)))

        assert rows.stops == (1.0, 2.0)
        assert rows.markets == ["e A H1"]
        assert rows.r.shape == (1, 2)
        # Paid 0.1 in price: a tenth of the 1x stop's risk, a twentieth of the 2x one's.
        assert rows.cost_r[0] == pytest.approx([0.1, 0.05])

    def test_stops_out_of_order_are_refused(self) -> None:
        rows = entry("A", at(2021), {1.0: -1.0, 2.0: 0.5})

        with pytest.raises(ValueError, match="in the grid's order"):
            grid_rows(table(rows, entry("A", at(2022), {2.0: 0.5, 1.0: -1.0})))

    def test_a_block_with_two_entries_is_refused(self) -> None:
        mixed = entry("A", at(2021), {1.0: -1.0, 2.0: 0.5})
        mixed[1]["entry_time"] = at(2022)

        with pytest.raises(ValueError, match="disagree on entry_time"):
            grid_rows(table(mixed))


class TestACell:
    def test_a_target_reached_closes_there_less_what_the_trade_paid(self) -> None:
        rows = grid_rows(
            table(entry("A", at(2021), {1.0: -1.0, 2.0: -1.0}, mfe={1.0: 3.2, 2.0: 1.5}, costs=0.2))
        )

        hit = outcome(rows, 3.0, taken(rows, None))

        assert hit[0] == pytest.approx([3.0 - 0.2, -1.0]), "1x reached 3R; 2x only 1.5R of its own"

    def test_the_floor_is_read_stop_by_stop(self) -> None:
        """A tick of 0.25: the 1x stop is 4 ticks away, below the floor of 5; the 2x one is 8."""
        rows = grid_rows(
            table(entry("A", at(2021), {1.0: -1.0, 2.0: -1.0})),
            {"A": Market(tick_size=0.25, tick_value=0.25)},
        )

        assert taken(rows, Floor(min_ticks=5, min_spreads=2))[0].tolist() == [False, True]

    def test_a_refused_cell_is_no_trade(self) -> None:
        rows = grid_rows(
            table(entry("A", at(2021), {1.0: -1.0, 2.0: 0.0}, reasons={2.0: "refused"}))
        )

        assert taken(rows, None)[0].tolist() == [True, False]
        assert np.isnan(outcome(rows, None, taken(rows, None))[0, 1])


def test_an_account_books_the_trades_in_the_order_they_closed() -> None:
    realised = [-1.0, -1.0, 2.0, -1.0, -1.0, -1.0, 3.0]
    closed = np.asarray([0, 1, 2, 3, 4, 5, 6], dtype=np.int64)[::-1].copy()

    booked = summarise(np.asarray(realised[::-1]), closed)

    assert (booked.trades, booked.total_r, booked.longest_losing_streak) == (7, 0.0, 3)
    assert booked.max_drawdown_r == pytest.approx(3.0), "from 0 to -3 after the third loss in a row"
    assert booked.win_rate == pytest.approx(2 / 7)
    assert summarise(np.asarray([np.nan]), np.asarray([0], dtype=np.int64)) == EMPTY


def base(*, open_cell: bool = True) -> pa.Table:
    """Two markets, forty training entries each and ten of validation.

    A loses its stop every time with the setup's stop and makes half an R with twice it; B loses
    under both. One more A entry of validation is still open under the wider stop."""
    entries = []
    for symbol, wide in (("A", 0.5), ("B", -0.5)):
        for n in range(40):
            entries.append(entry(symbol, at(2021 + n // 20, n), {1.0: -1.0, 2.0: wide}))
        for n in range(10):
            entries.append(entry(symbol, at(2023, n), {1.0: -1.0, 2.0: wide}))
    if open_cell:
        entries.append(entry("A", at(2023, 50), {1.0: -1.0, 2.0: 0.0}, reasons={2.0: "open"}))
    return table(*entries)


class TestTheRules:
    def test_each_market_trades_the_cell_its_training_years_paid_and_none_if_none_did(
        self,
    ) -> None:
        report = evaluate(grid_rows(base()), VALIDATION, targets=(None,))

        assert report.left_open == 1, "left out of every cell, not just the open one"
        assert report.best_cell == "2x as run"
        rules = {rule.name: rule.validation for rule in report.rules}
        assert (rules["every entry"].trades, rules["every entry"].mean_r) == (20, -1.0)
        assert rules["markets that paid"] == EMPTY, "the setup's own stop paid nowhere"
        assert rules["one cell (2x as run)"].total_r == pytest.approx(0.0)
        assert rules["a cell per market"].trades == 10
        assert rules["a cell per market"].total_r == pytest.approx(5.0)
        choices = {choice.market: choice for choice in report.markets}
        assert choices["e A H1"].cell == "2x as run"
        assert choices["e A H1"].as_run_validation.total_r == pytest.approx(-10.0)
        assert choices["e B H1"].cell is None
        assert choices["e B H1"].validation == EMPTY

    def test_a_market_with_too_few_training_trades_is_not_chosen_for(self) -> None:
        report = evaluate(grid_rows(base()), VALIDATION, min_trades=50, targets=(None,))

        assert all(choice.cell is None for choice in report.markets)

    def test_an_entry_still_open_under_a_stop_when_validation_began_is_embargoed(self) -> None:
        late = entry(
            "A",
            dt.datetime(2022, 12, 20, tzinfo=dt.UTC),
            {1.0: -1.0, 2.0: 0.5},
            lasts=dt.timedelta(days=60),
        )

        rows = grid_rows(pa.concat_tables([base(open_cell=False), table(late)]))
        report = evaluate(rows, VALIDATION, targets=(None,))

        assert report.embargoed == 1

    def test_a_grid_without_the_setups_stop_is_refused(self) -> None:
        rows = grid_rows(table(entry("A", at(2021), {2.0: 0.5}), entry("A", at(2023), {2.0: 0.5})))

        with pytest.raises(ValueError, match="own stop"):
            evaluate(rows, VALIDATION)


def test_the_report_is_written_beside_the_base(tmp_path: Path) -> None:
    pq.write_table(base(), tmp_path / "grid.parquet")

    name, report = write_grid(tmp_path, VALIDATION, min_trades=30)

    assert name == "grid-val2023-2023-min30"
    written = tmp_path / "grid" / name
    kept = json.loads((written / "report.json").read_text())
    assert kept["report"]["best_cell"] == report.best_cell
    assert len(kept["manifest"]["grid_sha256"]) == 64
    text = (written / "report.md").read_text(encoding="utf-8")
    assert "a cell per market" in text
    assert "| e A H1 |" in text


def test_rows_keep_their_shape_when_taken() -> None:
    rows = grid_rows(base())

    kept = rows.take(rows.year == 2023)

    assert isinstance(kept, GridRows)
    assert kept.r.shape == (21, 2)

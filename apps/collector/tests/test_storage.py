"""Parquet: the layout, the partitioning, and prices that survive the round trip."""

import datetime as dt
from decimal import Decimal
from pathlib import Path

import pyarrow.dataset as pads
import pytest

from tradeforge_collector.source import Candle
from tradeforge_collector.storage import (
    OHLCV_SCHEMA,
    dataset_path,
    normalise,
    read_candles,
    write_candles,
)


def a_candle(moment: dt.datetime, close: str = "1.10500") -> Candle:
    return Candle(
        time=moment,
        open=Decimal("1.10000"),
        high=Decimal("1.11000"),
        low=Decimal("1.09000"),
        close=Decimal(close),
        tick_volume=100,
        spread=2,
        real_volume=0,
    )


def test_prices_come_back_exactly_as_they_went_in(tmp_path: Path) -> None:
    """The reason the schema uses decimal128 and not float64.

    A float64 round trip would return 1.1050000000000000710542735760100185871124267578125.
    Close enough for a chart; not close enough to be multiplied by a volume ten thousand
    times and still agree with the broker.
    """
    candle = a_candle(dt.datetime(2024, 3, 1, 9, tzinfo=dt.UTC), close="1.10525")

    write_candles(tmp_path, "EURUSD", "H1", [candle])
    [read_back] = read_candles(tmp_path, "EURUSD", "H1")

    assert read_back.close == Decimal("1.10525")
    assert isinstance(read_back.close, Decimal)
    assert read_back == candle


def test_timestamps_come_back_in_utc(tmp_path: Path) -> None:
    moment = dt.datetime(2024, 3, 1, 9, tzinfo=dt.UTC)

    write_candles(tmp_path, "EURUSD", "H1", [a_candle(moment)])
    [read_back] = read_candles(tmp_path, "EURUSD", "H1")

    assert read_back.time == moment
    assert read_back.time.tzinfo is dt.UTC


def test_the_layout_is_symbol_timeframe_year(tmp_path: Path) -> None:
    """Partition pruning depends on this shape: ask for 2024 and 2023 is never opened."""
    write_candles(
        tmp_path,
        "EURUSD",
        "H1",
        [
            a_candle(dt.datetime(2023, 12, 29, 9, tzinfo=dt.UTC)),
            a_candle(dt.datetime(2024, 1, 2, 9, tzinfo=dt.UTC)),
        ],
    )

    assert (tmp_path / "symbol=EURUSD" / "timeframe=H1" / "year=2023").is_dir()
    assert (tmp_path / "symbol=EURUSD" / "timeframe=H1" / "year=2024").is_dir()


def test_rewriting_a_year_replaces_it_instead_of_duplicating_it(tmp_path: Path) -> None:
    """Idempotence. A backfill is re-run — nightly, or after a gap turns up.

    Without `delete_matching`, the second run would append a second copy of every candle
    and the backtest would trade each bar twice.
    """
    moment = dt.datetime(2024, 3, 1, 9, tzinfo=dt.UTC)

    write_candles(tmp_path, "EURUSD", "H1", [a_candle(moment, close="1.10500")])
    write_candles(tmp_path, "EURUSD", "H1", [a_candle(moment, close="1.20500")])

    candles = read_candles(tmp_path, "EURUSD", "H1")

    assert len(candles) == 1
    assert candles[0].close == Decimal("1.20500")


def test_rewriting_one_year_leaves_the_others_alone(tmp_path: Path) -> None:
    """Re-fetching 2024 must not delete the 2023 you already have."""
    write_candles(tmp_path, "EURUSD", "H1", [a_candle(dt.datetime(2023, 6, 1, 9, tzinfo=dt.UTC))])
    write_candles(tmp_path, "EURUSD", "H1", [a_candle(dt.datetime(2024, 6, 1, 9, tzinfo=dt.UTC))])

    assert len(read_candles(tmp_path, "EURUSD", "H1")) == 2


def test_symbols_do_not_leak_into_each_other(tmp_path: Path) -> None:
    write_candles(tmp_path, "EURUSD", "H1", [a_candle(dt.datetime(2024, 6, 1, 9, tzinfo=dt.UTC))])
    write_candles(tmp_path, "GBPUSD", "H1", [a_candle(dt.datetime(2024, 6, 1, 9, tzinfo=dt.UTC))])

    assert len(read_candles(tmp_path, "EURUSD", "H1")) == 1
    assert len(read_candles(tmp_path, "GBPUSD", "H1")) == 1


def test_reading_a_symbol_that_was_never_collected_is_empty_not_an_error(tmp_path: Path) -> None:
    assert read_candles(tmp_path, "NOPE", "H1") == []


def test_writing_nothing_is_an_error_not_a_silent_no_op(tmp_path: Path) -> None:
    """An empty backfill means something went wrong upstream. It must not look like success."""
    with pytest.raises(ValueError, match="no candles"):
        write_candles(tmp_path, "EURUSD", "H1", [])


def test_the_partition_columns_are_not_repeated_inside_the_files(tmp_path: Path) -> None:
    """They live in the directory names — storing them per row would waste the whole point."""
    write_candles(tmp_path, "EURUSD", "H1", [a_candle(dt.datetime(2024, 6, 1, 9, tzinfo=dt.UTC))])

    files = list(tmp_path.rglob("*.parquet"))
    assert files
    columns = pads.dataset(files[0], format="parquet").schema.names

    assert "symbol" not in columns
    assert "year" not in columns
    assert "close" in columns


def test_dataset_path_is_the_directory_the_catalogue_records(tmp_path: Path) -> None:
    path = dataset_path(tmp_path, "EURUSD", "H1")

    assert path.endswith("symbol=EURUSD/timeframe=H1")


def test_the_schema_stores_prices_as_exact_decimals() -> None:
    for column in ("open", "high", "low", "close"):
        field = OHLCV_SCHEMA.field(column)
        assert field.type.scale == 10, column


def test_normalise_rounds_a_float_back_to_what_the_market_printed() -> None:
    """`Decimal(str(1.10525))` is exact; `Decimal(1.10525)` carries the float's noise."""
    assert normalise(Decimal("1.105249999"), 5) == Decimal("1.10525")
    assert normalise(Decimal("190.004"), 2) == Decimal("190.00")

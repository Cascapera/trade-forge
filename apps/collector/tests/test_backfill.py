"""The backfill, end to end, with no MetaTrader and no database.

This is what the `MarketDataSource` protocol buys: the actual product — download,
store, detect gaps, report — runs in the unit job, on Linux, on every push.
"""

import datetime as dt
from pathlib import Path

import pytest

from tradeforge_collector.backfill import backfill
from tradeforge_collector.gaps import anomalies
from tradeforge_collector.source import Candle
from tradeforge_collector.storage import read_candles
from tradeforge_collector.synthetic import SyntheticSource

JAN = dt.datetime(2024, 1, 1, tzinfo=dt.UTC)
FEB = dt.datetime(2024, 2, 1, tzinfo=dt.UTC)


def test_a_backfill_writes_parquet_and_reports_what_it_did(tmp_path: Path) -> None:
    report = backfill(
        SyntheticSource(), root=tmp_path, symbol="EURUSD", timeframe="H1", start=JAN, end=FEB
    )

    assert report.candles > 500
    assert report.instrument.symbol == "EURUSD"
    assert report.date_from >= JAN
    assert report.date_to <= FEB
    assert len(read_candles(tmp_path, "EURUSD", "H1")) == report.candles


def test_running_it_twice_leaves_one_copy(tmp_path: Path) -> None:
    """Idempotence, at the level a user experiences it."""
    first = backfill(
        SyntheticSource(), root=tmp_path, symbol="EURUSD", timeframe="H1", start=JAN, end=FEB
    )
    backfill(SyntheticSource(), root=tmp_path, symbol="EURUSD", timeframe="H1", start=JAN, end=FEB)

    assert len(read_candles(tmp_path, "EURUSD", "H1")) == first.candles


def test_the_weekends_are_reported_but_not_as_anomalies(tmp_path: Path) -> None:
    """A month of hourly forex has four weekends in it and nothing actually wrong."""
    report = backfill(
        SyntheticSource(), root=tmp_path, symbol="EURUSD", timeframe="H1", start=JAN, end=FEB
    )

    assert len(report.gaps) >= 4
    assert anomalies(report.gaps) == []


def test_a_naive_datetime_is_refused(tmp_path: Path) -> None:
    """ "2024-01-01 00:00" is not an instant until you say where."""
    with pytest.raises(ValueError, match="timezone-aware"):
        backfill(
            SyntheticSource(),
            root=tmp_path,
            symbol="EURUSD",
            timeframe="H1",
            start=dt.datetime(2024, 1, 1),  # noqa: DTZ001 — the mistake under test
            end=FEB,
        )


def test_a_backwards_range_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="before start"):
        backfill(
            SyntheticSource(), root=tmp_path, symbol="EURUSD", timeframe="H1", start=FEB, end=JAN
        )


def test_an_empty_result_is_an_error_not_an_empty_dataset(tmp_path: Path) -> None:
    """A backfill that found nothing must not be recorded as a dataset that has nothing."""
    saturday = dt.datetime(2024, 1, 6, tzinfo=dt.UTC)
    sunday = dt.datetime(2024, 1, 7, 23, tzinfo=dt.UTC)

    with pytest.raises(LookupError, match="no candles"):
        backfill(
            SyntheticSource(),
            root=tmp_path,
            symbol="EURUSD",
            timeframe="H1",
            start=saturday,
            end=sunday,
        )


class _OutOfOrderSource:
    """A broken source: the bug that would hand the engine a lookahead."""

    def __init__(self) -> None:
        self._real = SyntheticSource()

    def instrument(self, symbol: str) -> object:
        return self._real.instrument(symbol)

    def candles(
        self, symbol: str, timeframe: str, start: dt.datetime, end: dt.datetime
    ) -> list[Candle]:
        candles = self._real.candles(symbol, timeframe, start, end)
        return list(reversed(candles))


def test_a_source_that_returns_candles_out_of_order_is_rejected(tmp_path: Path) -> None:
    """Checked, not silently sorted.

    Sorting here would keep a broken source working, and the next one that breaks
    differently would go unnoticed too. The engine's whole notion of "the previous
    candle" rests on this order.
    """
    with pytest.raises(ValueError, match="not ascending"):
        backfill(
            _OutOfOrderSource(),  # type: ignore[arg-type]  # deliberately breaks the contract
            root=tmp_path,
            symbol="EURUSD",
            timeframe="H1",
            start=JAN,
            end=FEB,
        )

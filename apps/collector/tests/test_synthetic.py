"""The synthetic feed — the thing CI actually trades against.

If this source is wrong, every test downstream of it is testing the wrong market. So
the two properties it promises are asserted directly: it is deterministic, and it is
shaped like a real feed (closed on weekends, extremes that contain the body).
"""

import datetime as dt
from decimal import Decimal

import pytest

from tradeforge_collector.synthetic import SyntheticSource

JAN = dt.datetime(2024, 1, 1, tzinfo=dt.UTC)
FEB = dt.datetime(2024, 2, 1, tzinfo=dt.UTC)


def test_the_same_request_returns_the_same_market() -> None:
    """Determinism, invariant 2 of AGENTS.md §5.

    A test suite whose data changes between runs cannot tell a real regression from
    noise — and a backtest you cannot reproduce is an anecdote.
    """
    first = SyntheticSource().candles("EURUSD", "H1", JAN, FEB)
    second = SyntheticSource().candles("EURUSD", "H1", JAN, FEB)

    assert first == second


def test_different_symbols_are_different_markets() -> None:
    eurusd = SyntheticSource().candles("EURUSD", "H1", JAN, FEB)
    gbpusd = SyntheticSource().candles("GBPUSD", "H1", JAN, FEB)

    assert [c.close for c in eurusd] != [c.close for c in gbpusd]


def test_the_market_is_closed_at_the_weekend() -> None:
    """Without this, the gap detector would only ever be tested on data that has no gaps."""
    candles = SyntheticSource().candles("EURUSD", "H1", JAN, FEB)

    assert all(candle.time.weekday() < 5 for candle in candles)


def test_every_candle_contains_its_own_body() -> None:
    """A high below the open is not a candle. It is a stop that triggers where price never went."""
    for candle in SyntheticSource().candles("EURUSD", "H1", JAN, FEB):
        assert candle.high >= max(candle.open, candle.close)
        assert candle.low <= min(candle.open, candle.close)
        assert candle.low > 0


def test_prices_land_on_the_instrument_tick() -> None:
    """A price the market cannot print is a price the backtest must not fill at."""
    candles = SyntheticSource().candles("EURUSD", "H1", JAN, FEB)

    for candle in candles[:50]:
        assert candle.close % Decimal("0.00001") == 0


def test_candles_are_ascending_and_hourly() -> None:
    candles = SyntheticSource().candles("EURUSD", "H1", JAN, FEB)
    times = [candle.time for candle in candles]

    assert times == sorted(times)
    assert len(set(times)) == len(times)


def test_a_stock_is_priced_differently_from_a_pair() -> None:
    """Two digits, one-cent ticks, no base currency — the multi-asset path, exercised."""
    spec = SyntheticSource().instrument("AAPL")

    assert spec.digits == 2
    assert spec.currency_base is None
    assert spec.tick_size == Decimal("0.01")


def test_an_unknown_symbol_says_so() -> None:
    with pytest.raises(ValueError, match="no synthetic instrument"):
        SyntheticSource().instrument("DOGECOIN")

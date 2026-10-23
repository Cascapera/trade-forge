"""Which bars mark a member's open positions — by hand, on H1 bars."""

import datetime as dt
from decimal import Decimal

from tradeforge_api.cluster_job import _marks
from tradeforge_engine.domain import Candle

START = dt.datetime(2024, 1, 1, tzinfo=dt.UTC)
HOUR = dt.timedelta(hours=1)


def bar(n: int, close: str = "1.1") -> Candle:
    price = Decimal(close)
    return Candle(time=START + n * HOUR, open=price, high=price, low=price, close=price)


def test_only_the_closes_strictly_inside_a_trade_mark_it() -> None:
    """A trade from 02:00 to 05:00: bars opening at 02:00, 03:00 close at 03:00, 04:00 — inside.
    The bar closing at 05:00 closes with the trade, which its exit prices; the one closing at
    02:00 is before it."""
    candles = [bar(n) for n in range(8)]

    found = _marks(0, candles, "H1", [(START + 2 * HOUR, START + 5 * HOUR)])

    assert [mark.time for mark in found] == [START + 3 * HOUR, START + 4 * HOUR]


def test_several_trades_are_walked_in_time_order() -> None:
    candles = [bar(n, close=f"1.{n}") for n in range(12)]
    spans = [(START + 8 * HOUR, START + 10 * HOUR), (START + 1 * HOUR, START + 3 * HOUR)]

    found = _marks(3, candles, "H1", spans)

    assert [(mark.member, mark.time, mark.close) for mark in found] == [
        (3, START + 2 * HOUR, Decimal("1.1")),
        (3, START + 9 * HOUR, Decimal("1.8")),
    ]


def test_no_trades_is_no_marks() -> None:
    assert _marks(0, [bar(n) for n in range(4)], "H1", []) == []

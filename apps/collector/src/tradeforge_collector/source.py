"""Where candles come from — the seam that keeps MetaTrader out of the test suite.

`MetaTrader5` ships Windows wheels only, so on Linux CI the library does not merely
fail to connect: it cannot be installed at all (ADR-02, enforced by the platform
marker in `pyproject.toml` and by `tests/test_architecture.py`). Every line of the
backfill therefore has to be exercisable without it.

The answer is a `Protocol`. `backfill()` depends on this interface, never on MT5;
`MT5Source` implements it on a Windows box, `SyntheticSource` implements it anywhere.
That is the same shape as `check_postgres(connect=...)` in the API — the logic runs
against a fake, the wiring runs against the real thing, and neither pretends to be
the other.

Structural, not nominal: a class satisfies `MarketDataSource` by having the right
methods. Nothing inherits from anything, and the collector never imports the classes
that implement it.
"""

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

from tradeforge_db.instruments import InstrumentSpec


@dataclass(frozen=True, slots=True)
class Candle:
    """One closed OHLCV bar.

    `time` is the bar's **opening** instant, in **UTC**, always. Both halves of that
    sentence are load-bearing:

    * *Opening*, because the alternative — labelling a bar by its close — makes every
      off-by-one in the engine invisible. The anti-lookahead rule (AGENTS.md §5.1) is
      stated in terms of "the candle closed at N", and N must mean one thing.
    * *UTC*, because MT5 hands out the broker's server time, which is typically UTC+2
      or UTC+3 *and observes daylight saving*. Left unconverted, every backtest is
      quietly shifted by hours, and the shift is not even constant across the year.

    Prices are `Decimal`, quantised to the instrument's tick. See `storage.py` for why
    they stay exact all the way into Parquet.
    """

    time: dt.datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    tick_volume: int
    spread: int
    real_volume: int


class MarketDataSource(Protocol):
    """Anything that can describe a symbol and hand over its history."""

    def instrument(self, symbol: str) -> InstrumentSpec:
        """The contract specification: tick size, tick value, digits, currencies."""
        ...

    def candles(
        self, symbol: str, timeframe: str, start: dt.datetime, end: dt.datetime
    ) -> list[Candle]:
        """Closed bars in `[start, end]`, ascending, in UTC.

        Implementations guarantee all three. A source that returns bars out of order,
        or that includes the bar still forming, would hand the engine a lookahead it
        has no way to detect.
        """
        ...

"""Where candles come from — the seam that keeps MetaTrader out of the test suite.

`MetaTrader5` ships Windows wheels only, so on Linux CI the library does not merely fail
to connect: it cannot be installed at all (ADR-02, enforced by the platform marker in
`pyproject.toml` and by `tests/test_architecture.py`). Every line of the backfill therefore
has to be exercisable without it.

The answer is a `Protocol`. `backfill()` depends on this interface, never on MT5;
`MT5Source` implements it on a Windows box, `SyntheticSource` implements it anywhere. That
is the same shape as `check_postgres(connect=...)` in the API — the logic runs against a
fake, the wiring runs against the real thing, and neither pretends to be the other.

Structural, not nominal: a class satisfies `MarketDataSource` by having the right methods.
Nothing inherits from anything, and the collector never imports the classes that implement
it.

`Candle` itself is **not** defined here. It belongs to `packages/engine`, because the core
owns the vocabulary and the adapters conform to it — a candle shaped by whatever the
collector found convenient is a candle the engine would have to translate forever.
"""

import datetime as dt
from typing import Protocol

from tradeforge_engine.domain import Candle, InstrumentSpec

__all__ = ["Candle", "InstrumentSpec", "MarketDataSource"]


class MarketDataSource(Protocol):
    """Anything that can describe a symbol and hand over its history."""

    def instrument(self, symbol: str) -> InstrumentSpec:
        """The contract specification: tick size, tick value, digits, currencies."""
        ...

    def candles(
        self, symbol: str, timeframe: str, start: dt.datetime, end: dt.datetime
    ) -> list[Candle]:
        """Closed bars in `[start, end]`, ascending, in UTC.

        Implementations guarantee all three. A source that returns bars out of order, or
        that includes the bar still forming, would hand the engine a lookahead it has no
        way to detect.
        """
        ...

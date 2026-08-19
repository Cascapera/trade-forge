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
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

from tradeforge_engine.domain import Candle, InstrumentSpec

__all__ = ["Candle", "InstrumentSpec", "MarketDataSource", "SymbolInfo"]


@dataclass(frozen=True, slots=True)
class SymbolInfo:
    """One line of a broker's catalogue: enough to find a symbol, not enough to price it.

    ⚠️ **Deliberately not an `InstrumentSpec`.** That type is the engine's, and every field on
    it — tick size, tick value, contract size — is a number the engine multiplies money by. To
    fill one in you have to have decided the symbol's asset class, and the collector refuses to
    guess that (see `asset_class_from_path`, which returns `None` rather than picking). A
    catalogue listing must not require that decision: the whole point is to show a user the
    9550 symbols the broker offers *before* anyone has committed to collecting any of them.

    So this is the shallow view, and it is what a search result is made of. Turning one into an
    `InstrumentSpec` is the deliberate act of cataloguing, and it happens later, per symbol.
    """

    symbol: str
    description: str | None = None
    path: str | None = None
    digits: int | None = None
    visible: bool = False
    """Whether the symbol sits in the terminal's Market Watch.

    Reported, never used to filter. Measured on this project's broker on 19/08/2026: 74 of 84
    symbols are outside Market Watch, and every one of them answers `symbols_get(group=...)`
    and hands over history exactly like a selected one. Only the live loop has to select.
    """


class MarketDataSource(Protocol):
    """Anything that can describe a symbol and hand over its history."""

    def instrument(self, symbol: str) -> InstrumentSpec:
        """The contract specification: tick size, tick value, digits, currencies."""
        ...

    def spread_points(self, symbol: str) -> Decimal | None:
        """The broker's quoted spread in **ticks**, or `None` if this source cannot say.

        Deliberately not part of `InstrumentSpec`. That type is the engine's, and the engine
        prices a move without ever consulting a spread — costs reach a run as a plugged-in
        `CostModel` (ADR-07). This is catalogue data used to *pre-fill* that choice, so it
        travels beside the spec rather than inside it.

        In ticks, not in whatever unit the venue quotes. MT5 counts spreads in `point`, which
        is not the same quantity as `trade_tick_size` by definition, and the engine's
        `SpreadCostModel` counts ticks. Converting at the edge means no reader downstream has
        to know which unit it is holding.

        `None`, never zero, when unknown: zero is the claim that this instrument is free to
        trade, and a source that simply has no idea must not make it.

        Declared with no body even though every source but one answers `None`. A default here
        would not have saved anyone the line: conformance to a `Protocol` is *structural*, so
        a class satisfies it by having the method, and inheriting a default requires actually
        subclassing — which nothing here does, on purpose. Writing `return None` above would
        therefore have failed exactly the same way while looking like it worked.
        """
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

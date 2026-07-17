"""A market that does not exist, and behaves itself.

This is what the tests, the CI and a developer without MetaTrader run against. It is
not a toy: it is the reason the backfill, the Parquet layout, the gap report and the
catalogue are all exercised on every push, on Linux, with no broker in sight.

Two properties matter, and both are deliberate:

* **Deterministic.** The seed is derived from (symbol, timeframe), so the same
  arguments always produce byte-identical candles. Anything else would make the
  backtest tests flaky for reasons that have nothing to do with the engine — and
  determinism is invariant 2 of AGENTS.md §5.
* **Closed on weekends.** A real forex feed has no Saturday bars. Generating them
  would mean the gap detector was only ever tested against data that has no gaps —
  and the first real backfill would be the first time that code ever ran.
"""

import datetime as dt
from decimal import Decimal
from random import Random

from tradeforge_collector.timeframes import step
from tradeforge_engine.domain import AssetClass, Candle, InstrumentSpec

# The market is shut from Friday evening to Sunday evening. Modelled as "no bar whose
# opening instant falls on a Saturday or a Sunday" — close enough to a real feed for
# the gap detector to have something honest to classify.
_SATURDAY = 5

SYNTHETIC_INSTRUMENTS: dict[str, InstrumentSpec] = {
    "EURUSD": InstrumentSpec(
        symbol="EURUSD",
        name="Euro vs US Dollar (synthetic)",
        asset_class=AssetClass.FOREX,
        currency_base="EUR",
        currency_quote="USD",
        tick_size=Decimal("0.00001"),
        tick_value=Decimal("1"),
        contract_size=Decimal("100000"),
        digits=5,
    ),
    "GBPUSD": InstrumentSpec(
        symbol="GBPUSD",
        name="Great Britain Pound vs US Dollar (synthetic)",
        asset_class=AssetClass.FOREX,
        currency_base="GBP",
        currency_quote="USD",
        tick_size=Decimal("0.00001"),
        tick_value=Decimal("1"),
        contract_size=Decimal("100000"),
        digits=5,
    ),
    "AAPL": InstrumentSpec(
        symbol="AAPL",
        name="Apple Inc. (synthetic)",
        asset_class=AssetClass.STOCK,
        exchange="NASDAQ",
        currency_quote="USD",
        tick_size=Decimal("0.01"),
        tick_value=Decimal("0.01"),
        contract_size=Decimal("1"),
        digits=2,
    ),
}

_STARTING_PRICE: dict[str, Decimal] = {
    "EURUSD": Decimal("1.10000"),
    "GBPUSD": Decimal("1.27000"),
    "AAPL": Decimal("190.00"),
}


class SyntheticSource:
    """A seeded random walk that satisfies `MarketDataSource`."""

    def __init__(self, *, volatility: float = 0.0008) -> None:
        # Relative step per bar. Small enough that a year of H1 stays in a plausible
        # range; large enough that a moving-average cross actually happens.
        self._volatility = volatility

    def instrument(self, symbol: str) -> InstrumentSpec:
        try:
            return SYNTHETIC_INSTRUMENTS[symbol]
        except KeyError:
            known = ", ".join(SYNTHETIC_INSTRUMENTS)
            raise ValueError(f"no synthetic instrument {symbol!r}; known: {known}") from None

    def candles(
        self, symbol: str, timeframe: str, start: dt.datetime, end: dt.datetime
    ) -> list[Candle]:
        spec = self.instrument(symbol)
        bar = step(timeframe)
        tick = spec.tick_size

        # Seeded on the arguments, not on the clock: same request, same market.
        random = Random(f"{symbol}:{timeframe}".encode().hex())  # noqa: S311 — not cryptography

        price = _STARTING_PRICE[symbol]
        candles: list[Candle] = []
        moment = start

        while moment <= end:
            if moment.weekday() >= _SATURDAY:
                moment += bar
                continue

            open_ = price
            close = _quantise(open_ * (1 + Decimal(str(random.gauss(0, self._volatility)))), tick)
            # A bar's extremes must contain its body — a high below its open is not a
            # candle, it is a bug that would let a stop trigger where price never went.
            wick = abs(close - open_) * Decimal(str(random.uniform(0.1, 1.5)))
            high = _quantise(max(open_, close) + wick, tick)
            low = _quantise(min(open_, close) - wick, tick)

            candles.append(
                Candle(
                    time=moment,
                    open=open_,
                    high=high,
                    low=low,
                    close=close,
                    tick_volume=random.randint(50, 5000),
                    spread=random.randint(1, 20),
                    real_volume=0,
                )
            )

            price = close
            moment += bar

        return candles


def _quantise(value: Decimal, tick: Decimal) -> Decimal:
    """Snap a price to the instrument's tick.

    A price the market cannot print is a price the backtest must not fill at.
    """
    return (value / tick).quantize(Decimal(1)) * tick

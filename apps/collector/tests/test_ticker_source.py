"""An internal name apart from the broker's ticker (ADR-0032): asked as `WIN$`, kept as `WIN`."""

import datetime as dt
from decimal import Decimal

from tradeforge_collector.source import Candle, InstrumentSpec, TickerSource
from tradeforge_engine.domain import AssetClass

START = dt.datetime(2026, 10, 1, tzinfo=dt.UTC)


class _Terminal:
    """Answers only to the broker's own tickers, and remembers what it was asked."""

    def __init__(self) -> None:
        self.asked: list[str] = []

    def instrument(self, symbol: str) -> InstrumentSpec:
        self.asked.append(symbol)
        return InstrumentSpec(
            symbol=symbol,
            name="IBOVESPA MINI",
            asset_class=AssetClass.FUTURE,
            currency_quote="BRL",
            tick_size=Decimal(1),
            tick_value=Decimal("0.2"),
            contract_size=Decimal(1),
            digits=0,
        )

    def spread_points(self, symbol: str) -> Decimal | None:
        self.asked.append(symbol)
        return Decimal(5)

    def candles(
        self, symbol: str, timeframe: str, start: dt.datetime, end: dt.datetime
    ) -> list[Candle]:
        self.asked.append(symbol)
        return [Candle(START, Decimal(1), Decimal(2), Decimal(1), Decimal(2), 10, 5, 0)]


def test_the_terminal_is_asked_for_the_ticker_and_the_spec_keeps_the_internal_name() -> None:
    terminal = _Terminal()
    source = TickerSource(terminal, internal="WIN", ticker="WIN$")

    spec = source.instrument("WIN")
    source.spread_points("WIN")
    bars = source.candles("WIN", "H1", START, START)

    assert spec.symbol == "WIN"
    assert spec.name == "IBOVESPA MINI"
    assert len(bars) == 1
    assert terminal.asked == ["WIN$", "WIN$", "WIN$"]


def test_any_other_symbol_passes_through_untouched() -> None:
    terminal = _Terminal()
    source = TickerSource(terminal, internal="WIN", ticker="WIN$")

    assert source.instrument("PETR4").symbol == "PETR4"
    assert terminal.asked == ["PETR4"]

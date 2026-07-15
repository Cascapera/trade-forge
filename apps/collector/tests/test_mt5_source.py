"""The MetaTrader edge, tested without MetaTrader.

`MT5Source` talks to a terminal that only exists on Windows. But the code that would
*corrupt* data rather than fail — shifting the broker's clock to UTC, turning a float
into the price the market actually printed, reading a contract specification — is
ordinary logic sitting behind that call. A fake terminal is enough to run all of it, on
Linux, on every push.

Note that this file never imports `MetaTrader5`. Neither does the module it tests, until
`connect()` is called without an injected terminal.
"""

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import pytest

from tradeforge_collector.mt5_source import MT5Source, asset_class_from_path, infer_server_offset
from tradeforge_engine.domain import AssetClass

UTC_NOON = dt.datetime(2024, 6, 3, 12, 0, tzinfo=dt.UTC)

# The broker's clock. Everything this fake reports is in UTC+3, which is what a real
# terminal does — and what the collector has to undo.
SERVER_OFFSET = dt.timedelta(hours=3)


@dataclass
class _SymbolInfo:
    name: str = "EURUSD"
    description: str = "Euro vs US Dollar"
    path: str = "Forex\\Majors\\EURUSD"
    exchange: str = ""
    currency_base: str = "EUR"
    currency_profit: str = "USD"
    trade_tick_size: float = 1e-05
    trade_tick_value: float = 1.0
    trade_contract_size: float = 100000.0
    digits: int = 5


class _FakeTerminal:
    """The slice of the MetaTrader 5 API that the collector actually uses."""

    TIMEFRAME_H1 = 16385

    def __init__(self, *, rates: list[dict[str, Any]] | None = None) -> None:
        self._rates = rates or []
        self.shutdown_called = False

    def initialize(self) -> bool:
        return True

    def shutdown(self) -> None:
        self.shutdown_called = True

    def last_error(self) -> tuple[int, str]:
        return (-1, "fake")

    def symbols_get(self) -> list[_SymbolInfo]:
        return [_SymbolInfo()]

    def symbol_info(self, symbol: str) -> _SymbolInfo | None:
        return _SymbolInfo(name=symbol) if symbol == "EURUSD" else None

    def symbol_info_tick(self, symbol: str) -> Any:  # noqa: ANN401, ARG002
        # A tick timestamped in the server's clock — which is the only way to find out
        # what that clock is.
        server_now = dt.datetime.now(tz=dt.UTC) + SERVER_OFFSET
        return type("Tick", (), {"time": int(server_now.timestamp())})()

    def copy_rates_range(
        self, _symbol: str, _timeframe: int, _start: dt.datetime, _end: dt.datetime
    ) -> list[dict[str, Any]] | None:
        # The signature is MetaTrader's. The fake returns whatever it was handed — the
        # filtering is the terminal's job, and not the thing under test.
        return self._rates


def a_rate(server_hour: int) -> dict[str, Any]:
    """One bar, timestamped the way MT5 does it: server local time, labelled as UTC."""
    server_time = dt.datetime(2024, 6, 3, server_hour, tzinfo=dt.UTC)
    return {
        "time": int(server_time.timestamp()),
        "open": 1.10525,
        "high": 1.10600,
        "low": 1.10500,
        "close": 1.10550,
        "tick_volume": 123,
        "spread": 2,
        "real_volume": 0,
    }


# --------------------------------------------------------------------------- #
# The pure functions — the two things that go wrong silently                     #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("Forex\\Majors\\EURUSD", AssetClass.FOREX),
        ("Stocks\\US\\AAPL", AssetClass.STOCK),
        ("Indices\\US500", AssetClass.INDEX),
        ("Crypto\\BTCUSD", AssetClass.CRYPTO),
        ("Futures\\CL", AssetClass.FUTURE),
    ],
)
def test_the_asset_class_is_read_from_the_symbol_tree(path: str, expected: AssetClass) -> None:
    assert asset_class_from_path(path) == expected


def test_an_unrecognised_path_returns_none_instead_of_guessing() -> None:
    """A symbol filed under the wrong class gets the wrong tick arithmetic.

    And wrong tick arithmetic is a P&L that is off by a constant factor — which looks
    exactly like a very good strategy. Better to stop and ask for `--asset-class`.
    """
    assert asset_class_from_path("CFD\\Exotic\\WHATEVER") is None


def test_the_server_clock_is_measured_and_rounded_to_the_half_hour() -> None:
    server_says = UTC_NOON + dt.timedelta(hours=3)

    assert infer_server_offset(server_says, UTC_NOON) == dt.timedelta(hours=3)


def test_clock_skew_between_the_two_machines_does_not_invent_an_offset() -> None:
    """A few seconds of drift must not round up into a whole hour of correction."""
    server_says = UTC_NOON + dt.timedelta(hours=2, seconds=17)

    assert infer_server_offset(server_says, UTC_NOON) == dt.timedelta(hours=2)


def test_a_server_already_on_utc_gets_no_correction() -> None:
    assert infer_server_offset(UTC_NOON, UTC_NOON) == dt.timedelta()


def test_a_server_behind_utc_is_handled_too() -> None:
    assert infer_server_offset(UTC_NOON - dt.timedelta(hours=5), UTC_NOON) == dt.timedelta(hours=-5)


# --------------------------------------------------------------------------- #
# The source, against a fake terminal                                           #
# --------------------------------------------------------------------------- #


def test_candles_are_shifted_out_of_the_brokers_clock_into_utc() -> None:
    """The bug this whole file exists to prevent.

    The terminal reports a bar at 15:00 on a server running UTC+3. That bar opened at
    12:00 UTC. Leave the shift out and nothing crashes — the backtest simply trades a
    market displaced by three hours, and the result looks entirely plausible.
    """
    terminal = _FakeTerminal(rates=[a_rate(server_hour=15)])

    with MT5Source(terminal=terminal) as source:
        [candle] = source.candles("EURUSD", "H1", UTC_NOON, UTC_NOON + dt.timedelta(days=1))

    assert candle.time == dt.datetime(2024, 6, 3, 12, tzinfo=dt.UTC)
    assert candle.time.tzinfo is dt.UTC


def test_prices_arrive_as_exact_decimals_quantised_to_the_tick() -> None:
    """MT5 hands out floats. `Decimal(1.10525)` would keep the float's binary noise forever."""
    terminal = _FakeTerminal(rates=[a_rate(server_hour=15)])

    with MT5Source(terminal=terminal) as source:
        [candle] = source.candles("EURUSD", "H1", UTC_NOON, UTC_NOON + dt.timedelta(days=1))

    assert candle.open == Decimal("1.10525")
    assert candle.close == Decimal("1.10550")


def test_the_contract_specification_is_read_from_the_terminal() -> None:
    with MT5Source(terminal=_FakeTerminal()) as source:
        spec = source.instrument("EURUSD")

    assert spec.asset_class == AssetClass.FOREX
    assert spec.digits == 5
    assert spec.tick_size == Decimal("0.00001")
    assert spec.contract_size == Decimal("100000")
    assert spec.currency_base == "EUR"
    assert spec.currency_quote == "USD"


def test_an_unknown_symbol_is_a_clear_error() -> None:
    with MT5Source(terminal=_FakeTerminal()) as source, pytest.raises(LookupError, match="EURJPY"):
        source.instrument("EURJPY")


def test_using_the_source_before_connecting_is_refused() -> None:
    with pytest.raises(RuntimeError, match="not connected"):
        MT5Source(terminal=_FakeTerminal()).instrument("EURUSD")


def test_leaving_the_context_shuts_the_terminal_down() -> None:
    """A terminal left open holds a lock on the MT5 installation."""
    terminal = _FakeTerminal()

    with MT5Source(terminal=terminal):
        pass

    assert terminal.shutdown_called


def test_a_timeframe_the_terminal_does_not_know_is_refused() -> None:
    with (
        MT5Source(terminal=_FakeTerminal()) as source,
        pytest.raises(ValueError, match="no timeframe"),
    ):
        source.candles("EURUSD", "W1", UTC_NOON, UTC_NOON)


def test_a_terminal_that_refuses_the_connection_says_so() -> None:
    class _Refusing(_FakeTerminal):
        def initialize(self) -> bool:
            return False

    with pytest.raises(ConnectionError, match="refused"):
        MT5Source(terminal=_Refusing()).connect()


def test_the_asset_class_can_be_overridden_when_the_path_says_nothing() -> None:
    class _Unclassifiable(_FakeTerminal):
        def symbol_info(self, symbol: str) -> _SymbolInfo | None:
            return _SymbolInfo(name=symbol, path="CFD\\Whatever\\X")

    with MT5Source(terminal=_Unclassifiable(), asset_class=AssetClass.INDEX) as source:
        assert source.instrument("EURUSD").asset_class == AssetClass.INDEX


def test_without_an_override_an_unclassifiable_symbol_stops_the_backfill() -> None:
    class _Unclassifiable(_FakeTerminal):
        def symbol_info(self, symbol: str) -> _SymbolInfo | None:
            return _SymbolInfo(name=symbol, path="CFD\\Whatever\\X")

    with (
        MT5Source(terminal=_Unclassifiable()) as source,
        pytest.raises(LookupError, match="asset-class"),
    ):
        source.instrument("EURUSD")

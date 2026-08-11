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

from tradeforge_collector.mt5_source import (
    MT5Source,
    asset_class_from_path,
    infer_server_offset,
    offset_is_plausible,
)
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
    # MT5 counts a spread in `point`, which is a *different field* from `trade_tick_size`.
    # They coincide on this project's own broker, which is exactly why the default here keeps
    # them equal — and why the tests below deliberately pull them apart.
    point: float = 1e-05
    spread: int = 12
    spread_float: bool = True


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

    def last_tick_age(self, _symbol: str) -> dt.timedelta:
        """How long ago this symbol last traded. Zero is a live, liquid market."""
        return dt.timedelta()

    def symbol_info_tick(self, symbol: str) -> Any:
        # A tick timestamped in the server's clock — which is the only way to find out
        # what that clock is. Subtracting the age is what lets a subclass go quiet:
        # a symbol that has not traded, or a market that has closed.
        server_now = dt.datetime.now(tz=dt.UTC) + SERVER_OFFSET - self.last_tick_age(symbol)
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


@pytest.mark.parametrize(
    "hours",
    [0, 3, -5, 5.5, 14, -14],
    ids=["utc", "ahead", "behind", "half hour", "furthest ahead", "furthest behind"],
)
def test_a_real_timezone_is_accepted(hours: float) -> None:
    assert offset_is_plausible(dt.timedelta(hours=hours))


@pytest.mark.parametrize(
    "hours",
    [-62, 62, 17.5, -48, 14.5],
    ids=[
        "the weekend that displaced a real backfill",
        "the same, mirrored",
        "a stock market's overnight",
        "a weekend",
        "just past the furthest timezone",
    ],
)
def test_a_closure_is_not_mistaken_for_a_timezone(hours: float) -> None:
    """The bug this whole change exists to prevent.

    -62h is not a hypothetical: it is what a real backfill measured on a Monday morning,
    sixteen minutes before the opening bell, with every tick in the terminal frozen at
    Friday's close. It shifted 3494 bars two and a half days into the future and reported
    success.
    """
    assert not offset_is_plausible(dt.timedelta(hours=hours))


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


class _PointIsTenTicks(_FakeTerminal):
    """A venue where one `point` is ten ticks — the case the coincidence hides."""

    def symbol_info(self, symbol: str) -> _SymbolInfo | None:
        if symbol != "EURUSD":
            return None
        return _SymbolInfo(name=symbol, point=1e-04, trade_tick_size=1e-05, spread=12)


class _NoPoint(_FakeTerminal):
    """A symbol the terminal has not selected into Market Watch: zeroes all the way down."""

    def symbol_info(self, symbol: str) -> _SymbolInfo | None:
        if symbol != "EURUSD":
            return None
        return _SymbolInfo(name=symbol, point=0.0, trade_tick_size=0.0)


def test_the_spread_is_read_in_ticks_not_in_the_brokers_points() -> None:
    # The default fake keeps point and tick equal, which is what this project's own broker
    # does — so here the two units agree and 12 points is 12 ticks.
    with MT5Source(terminal=_FakeTerminal()) as source:
        assert source.spread_points("EURUSD") == Decimal("12")


def test_a_point_worth_several_ticks_is_converted_rather_than_copied() -> None:
    """The assertion the happy path cannot make, because there the two units coincide.

    `spread` counts `point`; `SpreadCostModel` counts ticks. Copying the number across
    would be right on every symbol this project has today and wrong by a factor of ten on
    the first venue that sets a tick to a fraction of a point — a silent factor, since a
    cost model does not know what it should have charged.
    """
    with MT5Source(terminal=_PointIsTenTicks()) as source:
        assert source.spread_points("EURUSD") == Decimal("120")


def test_a_symbol_the_terminal_cannot_price_reports_unknown_not_free() -> None:
    # Zero would say "this instrument costs nothing to trade", which is a claim. None says
    # nobody measured it, and the screen is built to tell the reader which one it has.
    with MT5Source(terminal=_NoPoint()) as source:
        assert source.spread_points("EURUSD") is None


class _MarketShut(_FakeTerminal):
    """The exchange closed 47 minutes ago and the last quote is the widened closing print."""

    def last_tick_age(self, _symbol: str) -> dt.timedelta:
        return dt.timedelta(minutes=47)

    def symbol_info(self, symbol: str) -> _SymbolInfo | None:
        # What this broker actually reported for AAPL after the close: eleven times the
        # spread the same terminal quoted during the session.
        return _SymbolInfo(name=symbol, spread=110) if symbol == "EURUSD" else None


def test_a_spread_read_after_the_close_is_not_catalogued() -> None:
    """The bug this guard was written from, reproduced.

    Catalogued 11 ticks for AAPL 47 minutes after the US close, against the 1 tick the same
    terminal quoted during the session — brokers widen at the close, and `symbol_info.spread`
    keeps reporting the widened number for as long as the market stays shut. Stored, it would
    have charged eleven times the real cost under a column that reads as a measurement.
    """
    with MT5Source(terminal=_MarketShut(), server_offset=SERVER_OFFSET) as source:
        assert source.spread_points("EURUSD") is None


def test_a_live_quote_is_catalogued_even_though_the_symbol_is_quiet() -> None:
    """The other half: a gap of a couple of minutes is a thin market, not a shut one.

    Refusing those would trade a wrong number for a missing one on every illiquid symbol,
    which is why the threshold is minutes rather than seconds.
    """

    class _Quiet(_FakeTerminal):
        def last_tick_age(self, _symbol: str) -> dt.timedelta:
            return dt.timedelta(minutes=2)

    with MT5Source(terminal=_Quiet(), server_offset=SERVER_OFFSET) as source:
        assert source.spread_points("EURUSD") == Decimal("12")


def test_asking_the_spread_of_an_unknown_symbol_is_the_same_clear_error() -> None:
    with MT5Source(terminal=_FakeTerminal()) as source, pytest.raises(LookupError, match="EURJPY"):
        source.spread_points("EURJPY")


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


def test_a_range_the_terminal_has_no_data_for_is_a_clear_error() -> None:
    """`None` from MT5 means "I have nothing", and it must not become an empty dataset."""

    class _NoHistory(_FakeTerminal):
        def copy_rates_range(
            self, _symbol: str, _timeframe: int, _start: dt.datetime, _end: dt.datetime
        ) -> list[dict[str, Any]] | None:
            return None

    with (
        MT5Source(terminal=_NoHistory()) as source,
        pytest.raises(LookupError, match="no rates"),
    ):
        source.candles("EURUSD", "H1", UTC_NOON, UTC_NOON + dt.timedelta(days=1))


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


# --------------------------------------------------------------------------- #
# The server clock, when the market is not cooperating                          #
# --------------------------------------------------------------------------- #


class _ClosedMarket(_FakeTerminal):
    """Every tick frozen at Friday's close, read on Monday morning."""

    def last_tick_age(self, _symbol: str) -> dt.timedelta:
        return dt.timedelta(hours=62)


def test_a_closed_market_stops_the_backfill_instead_of_displacing_it() -> None:
    with pytest.raises(LookupError, match="server-offset"):
        MT5Source(terminal=_ClosedMarket()).connect()


def test_the_refusal_names_what_it_saw() -> None:
    """A user who cannot tell *why* it refused will reach for the flag with a guess."""
    with pytest.raises(LookupError, match="too far to be a timezone"):
        MT5Source(terminal=_ClosedMarket()).connect()


def test_a_stated_offset_makes_the_measurement_unnecessary() -> None:
    """The market is shut and the bars still land in the right place."""
    terminal = _ClosedMarket(rates=[a_rate(server_hour=15)])

    with MT5Source(terminal=terminal, server_offset=SERVER_OFFSET) as source:
        [candle] = source.candles("EURUSD", "H1", UTC_NOON, UTC_NOON + dt.timedelta(days=1))

    assert candle.time == dt.datetime(2024, 6, 3, 12, tzinfo=dt.UTC)


def test_the_clock_is_read_from_the_newest_tick_not_the_first_symbol() -> None:
    """An illiquid symbol is not evidence that the terminal is asleep.

    The first symbol here last traded nine hours ago — inside the plausible band, so the
    old first-match code would have accepted it and shifted every bar by nine hours. The
    market is live; one instrument in it simply is not.
    """

    class _OneStaleSymbol(_FakeTerminal):
        def symbols_get(self) -> list[_SymbolInfo]:
            # The live symbol sits in the *middle* deliberately: with it first, "take the
            # first tick" would pass this test, and with it last, "take the last one"
            # would. Only actually taking the maximum survives all three positions.
            return [
                _SymbolInfo(name="SLEEPY"),
                _SymbolInfo(name="EURUSD"),
                _SymbolInfo(name="DORMANT"),
            ]

        def last_tick_age(self, symbol: str) -> dt.timedelta:
            return dt.timedelta() if symbol == "EURUSD" else dt.timedelta(hours=9)

    terminal = _OneStaleSymbol(rates=[a_rate(server_hour=15)])

    with MT5Source(terminal=terminal) as source:
        [candle] = source.candles("EURUSD", "H1", UTC_NOON, UTC_NOON + dt.timedelta(days=1))

    assert candle.time == dt.datetime(2024, 6, 3, 12, tzinfo=dt.UTC)


def test_a_terminal_where_nothing_has_ever_ticked_is_refused() -> None:
    """Silence is not UTC. The old code logged a warning and assumed zero."""

    class _Mute(_FakeTerminal):
        def symbol_info_tick(self, symbol: str) -> Any:
            return None

    with pytest.raises(LookupError, match="has ever ticked"):
        MT5Source(terminal=_Mute()).connect()


def test_a_terminal_with_no_symbols_at_all_is_refused() -> None:
    class _Empty(_FakeTerminal):
        def symbols_get(self) -> list[_SymbolInfo]:
            return []

    with pytest.raises(LookupError, match="server-offset"):
        MT5Source(terminal=_Empty()).connect()


def test_a_stated_offset_that_is_not_a_timezone_is_refused_too() -> None:
    """`+30` is a typo whether a human typed it or a frozen tick implied it."""
    with pytest.raises(ValueError, match="is not a timezone"):
        MT5Source(terminal=_FakeTerminal(), server_offset=dt.timedelta(hours=30))

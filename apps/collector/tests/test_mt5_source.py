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

from tradeforge_collector import mt5_source
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
    visible: bool = True


class _FakeTerminal:
    """The slice of the MetaTrader 5 API that the collector actually uses."""

    TIMEFRAME_H1 = 16385

    def __init__(self, *, rates: list[dict[str, Any]] | None = None) -> None:
        self._rates = rates or []
        self.shutdown_called = False
        self.initialised = 0
        self.selected: list[str] = []
        self.from_pos: list[tuple[str, int, int]] = []
        # Two symbols, one of them outside Market Watch — which is the ordinary state of a real
        # terminal (measured: 74 of 84) and the case a `visible` filter would silently hide.
        self.catalogue = [
            _SymbolInfo(name="EURUSD", visible=True),
            _SymbolInfo(name="AUDCAD", description="", path="", visible=False),
        ]

    def initialize(self) -> bool:
        self.initialised += 1
        return True

    def shutdown(self) -> None:
        self.shutdown_called = True

    def terminal_info(self) -> object | None:
        """A live terminal answers with something; a dead one answers `None`.

        That is the only reliable way to tell "the terminal is gone" from "that symbol has no
        bars", because MetaTrader gives both of them the same shape of answer.
        """
        return object()

    def symbol_select(self, symbol: str, enable: bool) -> bool:
        self.selected.append(symbol)
        return enable

    def copy_rates_from_pos(
        self, symbol: str, timeframe: int, start_pos: int, count: int
    ) -> list[dict[str, Any]] | None:
        # Position 0 is the bar still forming, so a caller asking from position 1 gets the
        # closed ones — which is exactly what the fake's history is.
        self.from_pos.append((symbol, start_pos, count))
        return self._rates[-count:] if self._rates else None

    def last_error(self) -> tuple[int, str]:
        return (-1, "fake")

    def symbols_get(self) -> list[_SymbolInfo]:
        return self.catalogue

    def account_info(self) -> Any:
        return type("Account", (), {"server": "MetaQuotes-Demo"})()

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
    """⚠️ `ConnectionError`, because detached is a connection state.

    The live loop routes on the *type*: a `ConnectionError` is the feed being down and costs a
    reconnection, anything else is one symbol having a bad day and costs a log line. A source
    that is not attached belongs squarely in the first category — see the test below for the
    path that makes the difference load-bearing rather than pedantic.
    """
    with pytest.raises(ConnectionError, match="not connected"):
        MT5Source(terminal=_FakeTerminal()).instrument("EURUSD")


def test_a_reconnection_that_failed_leaves_a_source_the_loop_can_still_reach() -> None:
    """⚠️ **Found by running the acceptance against the real terminal**, not by a test.

    `reconnect()` clears the handle and then re-initialises. When the second half fails — a
    terminal that is still coming up, which is the *expected* case — the source stays detached.
    Every later poll then has to keep saying "the feed is down", because that is the only
    answer that buys another retry. Raising anything else meant the loop swallowed it as one
    bad symbol and never attempted a reconnection again: **one failed retry wedged the
    collector for good**, in silence.

    22 mutants missed this. The fake source in `test_live.py` keeps raising `ConnectionError`
    after a refused reconnection, so it agreed with the fixed code and with the broken code
    alike — the divergence only existed in the real adapter.
    """

    class _WontComeBack(_FakeTerminal):
        def initialize(self) -> bool:
            self.initialised += 1
            return self.initialised == 1

    source = MT5Source(terminal=_WontComeBack(), server_offset=SERVER_OFFSET).connect()

    with pytest.raises(ConnectionError, match="refused the connection"):
        source.reconnect()

    # And again, and again — every poll from here has to be a `ConnectionError`, or the loop
    # stops asking.
    for _ in range(3):
        with pytest.raises(ConnectionError, match="not connected"):
            source.recent_closed("EURUSD", "H1", 1)


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


# --------------------------------------------------------------------------- #
# Watching a market: selecting, polling by position, and losing the terminal    #
# --------------------------------------------------------------------------- #


def test_the_closed_bar_is_asked_for_by_position_and_never_from_a_clock() -> None:
    """⚠️ Position 1, count 1 — the whole of "did it close?".

    The alternative compares `bar.time + step <= now`, which needs the server offset, and that
    offset is measured from the newest tick: a market that stopped ticking freezes it, so the
    comparison drifts by the length of the closure with nothing raised. Measured on this
    project's broker: +134 min against a real +180, 46 minutes after the close.
    """
    terminal = _FakeTerminal(rates=[a_rate(10), a_rate(11)])

    with MT5Source(terminal=terminal, server_offset=SERVER_OFFSET) as source:
        found = source.recent_closed("EURUSD", "H1", 1)

    assert terminal.from_pos == [("EURUSD", 1, 1)]
    # Labelled in UTC by undoing the broker's clock — the offset still translates, it just
    # never decides.
    assert [one.time for one in found] == [dt.datetime(2024, 6, 3, 8, tzinfo=dt.UTC)]


def test_a_run_of_bars_comes_back_oldest_first() -> None:
    """The gap fill publishes in this order, and Redis refuses ids that do not increase.

    ⚠️ Newest-first would land the first bar and have every older one rejected as a
    duplicate — a stream one bar long where five were expected, and no error anywhere.
    """
    terminal = _FakeTerminal(rates=[a_rate(9), a_rate(10), a_rate(11)])

    with MT5Source(terminal=terminal, server_offset=SERVER_OFFSET) as source:
        found = source.recent_closed("EURUSD", "H1", 3)

    assert [one.time.hour for one in found] == [6, 7, 8]


def test_a_symbol_with_no_bars_is_an_answer_and_not_a_failure() -> None:
    # A session that has not produced its first bar. A live loop that crashed on this could
    # not be started before the opening bell.
    with MT5Source(terminal=_FakeTerminal(), server_offset=SERVER_OFFSET) as source:
        assert source.recent_closed("EURUSD", "H1", 1) == []


def test_a_terminal_that_has_gone_away_is_not_a_symbol_with_no_bars() -> None:
    """⚠️ The ambiguity this check exists to remove, and it has to be checked first.

    A dead terminal answers `symbol_info` with `None`, which this class turns into "that symbol
    is not available in this terminal" — wrong, convincing, and read by the live loop as one bad
    symbol rather than as the feed being down. So `terminal_info` is asked before the symbol is,
    and the answer is a `ConnectionError` that the loop knows how to wait out.
    """

    class _Gone(_FakeTerminal):
        def terminal_info(self) -> object | None:
            return None

        def symbol_info(self, symbol: str) -> _SymbolInfo | None:
            return None

    with (
        MT5Source(terminal=_Gone(), server_offset=SERVER_OFFSET) as source,
        pytest.raises(ConnectionError, match="not answering"),
    ):
        source.recent_closed("EURUSD", "H1", 1)


def test_a_poll_asks_for_at_least_one_bar() -> None:
    with (
        MT5Source(terminal=_FakeTerminal(), server_offset=SERVER_OFFSET) as source,
        pytest.raises(ValueError, match="at least one bar"),
    ):
        source.recent_closed("EURUSD", "H1", 0)


def test_subscribing_puts_the_symbol_into_market_watch() -> None:
    """⚠️ Measured: 5 of 9550 symbols were selected on this project's broker.

    An unselected symbol reports no bars for ever, which is also the honest answer for a market
    that has not closed one — so a watch on the wrong symbol looks exactly like a quiet Sunday
    until somebody checks by hand.
    """
    terminal = _FakeTerminal()

    with MT5Source(terminal=terminal, server_offset=SERVER_OFFSET) as source:
        source.subscribe("EURUSD")

    assert terminal.selected == ["EURUSD"]


def test_a_symbol_the_terminal_will_not_select_stops_the_watch() -> None:
    class _Refusing(_FakeTerminal):
        def symbol_select(self, symbol: str, enable: bool) -> bool:
            return False

    with (
        MT5Source(terminal=_Refusing(), server_offset=SERVER_OFFSET) as source,
        pytest.raises(LookupError, match="Market Watch"),
    ):
        source.subscribe("EURUSD")


def test_reconnecting_detaches_and_initialises_again() -> None:
    terminal = _FakeTerminal()
    source = MT5Source(terminal=terminal, server_offset=SERVER_OFFSET).connect()

    source.reconnect()

    assert terminal.shutdown_called is True
    assert terminal.initialised == 2


def test_a_shutdown_that_fails_still_lets_the_reconnection_happen() -> None:
    """⚠️ The one case that matters: reconnecting happens *after* the terminal died.

    That is precisely when `shutdown()` is most likely to throw, and a source that kept its old
    handle would believe it was still attached — so the next `connect()` would be layered on a
    connection that no longer exists.
    """

    class _BadShutdown(_FakeTerminal):
        def shutdown(self) -> None:
            raise OSError("the terminal is already gone")

    source = MT5Source(terminal=_BadShutdown(), server_offset=SERVER_OFFSET).connect()

    with pytest.raises(OSError, match="already gone"):
        source.close()

    # ⚠️ Asserted on the *detachment*, not on the reconnection. Going through
    # `reconnect()` proves nothing here: it swallows the failure and calls `connect()` either
    # way, so a source that kept its stale handle reattaches and the test passes. The
    # separating question is what the source believes right after a shutdown that threw.
    with pytest.raises(ConnectionError, match="not connected"):
        source.recent_closed("EURUSD", "H1", 1)


def test_a_failed_shutdown_does_not_stop_the_reconnection() -> None:
    """And the loop's side of it: `reconnect` survives a terminal that is already gone."""

    class _BadShutdown(_FakeTerminal):
        def shutdown(self) -> None:
            raise OSError("the terminal is already gone")

    terminal = _BadShutdown()
    source = MT5Source(terminal=terminal, server_offset=SERVER_OFFSET).connect()

    source.reconnect()

    assert terminal.initialised == 2
    assert source.recent_closed("EURUSD", "H1", 1) == []


def test_closing_a_source_that_was_never_connected_is_harmless() -> None:
    """Nothing to shut down, and the loop's cleanup path must not care whether there was."""
    MT5Source(terminal=_FakeTerminal(), server_offset=SERVER_OFFSET).close()


def test_the_catalogue_lists_symbols_outside_market_watch_too() -> None:
    """⚠️ Measured: 74 of 84 symbols on this broker are not in Market Watch, and every
    one of them answers a prefix query and hands over history exactly like a selected one.

    Filtering on `visible` would hide seven eighths of the catalogue to enforce a distinction
    only the live loop needs — and the user searching for a symbol to test is precisely the
    person who has not selected it yet.
    """
    with MT5Source(terminal=_FakeTerminal(), server_offset=SERVER_OFFSET) as source:
        found = source.symbols()

    assert [one.symbol for one in found] == ["EURUSD", "AUDCAD"]
    assert [one.visible for one in found] == [True, False]


def test_a_field_the_broker_left_blank_comes_back_as_unknown() -> None:
    """MetaTrader says "not set" with the empty string.

    ⚠️ Storing `""` would make "this broker gives no description" indistinguishable from
    "the description is blank", and the screen renders an empty line for both with no way to
    say which it is showing.
    """
    with MT5Source(terminal=_FakeTerminal(), server_offset=SERVER_OFFSET) as source:
        blank = next(one for one in source.symbols() if one.symbol == "AUDCAD")

    assert blank.description is None
    assert blank.path is None


def test_a_catalogue_read_from_a_dead_terminal_is_a_lost_connection() -> None:
    """Not an empty catalogue — which would be read as "this broker offers nothing"."""

    class _Gone(_FakeTerminal):
        def terminal_info(self) -> object | None:
            return None

    with (
        MT5Source(terminal=_Gone(), server_offset=SERVER_OFFSET) as source,
        pytest.raises(ConnectionError, match="not answering"),
    ):
        source.symbols()


def test_the_server_is_reported_so_a_symbol_list_carries_its_provenance() -> None:
    with MT5Source(terminal=_FakeTerminal(), server_offset=SERVER_OFFSET) as source:
        assert source.server() == "MetaQuotes-Demo"


def test_a_terminal_that_is_not_logged_in_has_no_server_name() -> None:
    """⚠️ Unknown, never a made-up name.

    A snapshot stamped with a plausible-looking server nobody is connected to is worse than one
    stamped `null`: the screen would show provenance that reads as verified.
    """

    class _LoggedOut(_FakeTerminal):
        def account_info(self) -> Any:
            return None

    with MT5Source(terminal=_LoggedOut(), server_offset=SERVER_OFFSET) as source:
        assert source.server() is None


# --------------------------------------------------------------------------- #
# The history probe                                                             #
# --------------------------------------------------------------------------- #


class _SeriesTerminal(_FakeTerminal):
    """A terminal holding a real series, addressable by position like MetaTrader's.

    Position 0 is the newest and counts into the past, and a request returns the `count` bars
    *ending* at `start_pos`, oldest first. Getting that direction wrong is how a probe reads the
    present when it means to read 1971 — which happened while writing these tests by hand.
    """

    TIMEFRAME_D1 = 16408
    TIMEFRAME_H1 = 16385

    def __init__(self, series: list[dict[str, Any]], *, maxbars: int = 100_000) -> None:
        super().__init__()
        self._series = series  # oldest first
        self._maxbars = maxbars

    def terminal_info(self) -> object:
        return type("Terminal", (), {"maxbars": self._maxbars})()

    def copy_rates_from_pos(
        self, symbol: str, timeframe: int, start_pos: int, count: int
    ) -> list[dict[str, Any]] | None:
        self.from_pos.append((symbol, start_pos, count))
        newest_first = list(reversed(self._series))
        window = newest_first[start_pos : start_pos + count]
        return list(reversed(window)) if window else None


def a_bar(
    when: dt.datetime, *, high: float, low: float, volume: int, spread: int
) -> dict[str, Any]:
    return {
        "time": int((when + SERVER_OFFSET).timestamp()),
        "open": low,
        "high": high,
        "low": low,
        "close": high,
        "tick_volume": volume,
        "spread": spread,
        "real_volume": 0,
    }


def a_series(
    *, filler_years: int, real_years: int, stamped_years: int, start: int = 2000
) -> list[dict[str, Any]]:
    """A history shaped like the one this project measured: filler, then market.

    `stamped_years` of the real era carry a constant spread, which is what a broker writing one
    number across a year looks like. Thirty bars a year keeps the fixture small while staying
    above `MIN_SPREAD_SAMPLES`.
    """
    bars: list[dict[str, Any]] = []
    year = start
    for _ in range(filler_years):
        for day in range(30):
            when = dt.datetime(year, 1, 1, tzinfo=dt.UTC) + dt.timedelta(days=day)
            bars.append(a_bar(when, high=0.5, low=0.5, volume=1, spread=20))
        year += 1
    for index in range(real_years):
        stamped = index < stamped_years
        for day in range(30):
            when = dt.datetime(year, 1, 1, tzinfo=dt.UTC) + dt.timedelta(days=day)
            bars.append(
                a_bar(when, high=1.2, low=1.1, volume=500, spread=20 if stamped else 8 + day % 5)
            )
        year += 1
    return bars


def test_the_probe_finds_the_depth_the_filler_and_the_costs() -> None:
    """One series carrying all three bounds at once, because they are independent.

    A fixture with only filler could not tell the cost floor from a default, and one with only
    stamped costs could not tell the filler floor from `None`.
    """
    series = a_series(filler_years=2, real_years=3, stamped_years=1)
    terminal = _SeriesTerminal(series)

    with MT5Source(terminal=terminal, server_offset=SERVER_OFFSET) as source:
        report = source.probe_history("EURUSD", "D1")

    assert report.bar_count == len(series)
    assert report.oldest == dt.datetime(2000, 1, 1, tzinfo=dt.UTC)
    # Two filler years, so 2001 is the last one; the real era starts in 2002 and its first year
    # carries a stamped spread, so costs are only measured from 2003.
    assert report.last_fabricated == 2001
    assert report.first_measured_cost == 2003


def test_a_series_with_no_filler_reports_none_rather_than_a_year() -> None:
    # BTCUSD measured exactly this: every year real, back to its first. `None` and not the
    # oldest year, which a reader would take as "there is filler up to here".
    terminal = _SeriesTerminal(a_series(filler_years=0, real_years=3, stamped_years=0))

    with MT5Source(terminal=terminal, server_offset=SERVER_OFFSET) as source:
        report = source.probe_history("EURUSD", "D1")

    assert report.last_fabricated is None
    assert report.first_measured_cost == 2000


def test_a_symbol_the_terminal_has_nothing_for_is_an_empty_report() -> None:
    """Not an error: a symbol the broker has never quoted is an ordinary state of the world."""
    with MT5Source(terminal=_SeriesTerminal([]), server_offset=SERVER_OFFSET) as source:
        report = source.probe_history("EURUSD", "D1")

    assert report.bar_count == 0
    assert report.oldest is None
    assert report.last_fabricated is None


def test_a_series_at_the_ceiling_says_whose_limit_it_is() -> None:
    # ⚠️ The number a person sizes a window from. Measured before this machine's `maxbars` was
    # raised: 100000 on four different timeframes, the same round number every time.
    series = a_series(filler_years=0, real_years=2, stamped_years=0)
    terminal = _SeriesTerminal(series, maxbars=len(series))

    with MT5Source(terminal=terminal, server_offset=SERVER_OFFSET) as source:
        report = source.probe_history("EURUSD", "D1")

    assert report.capped_by_terminal is True


def test_a_search_that_hits_its_own_ceiling_says_so(monkeypatch: pytest.MonkeyPatch) -> None:
    """⚠️ Seen for real: with `maxbars` raised to 100 million, EURUSD M1 answered every position
    the search asked for and came back as exactly 10,000,000 — the constant, not a measurement.

    The ceiling is lowered here rather than fed ten million bars, because its *value* is not the
    behaviour under test: what has to hold is that a count which is the search's own bound comes
    back marked, instead of arriving on screen as the number somebody sizes a window from.
    """
    monkeypatch.setattr(mt5_source, "_POSITION_CEILING", 8)
    series = a_series(filler_years=0, real_years=2, stamped_years=0)
    assert len(series) > 8, "the series must outrun the ceiling for the search to hit it"

    with MT5Source(terminal=_SeriesTerminal(series), server_offset=SERVER_OFFSET) as source:
        report = source.probe_history("EURUSD", "D1")

    assert report.bar_count_is_a_ceiling is True
    assert report.bar_count == 8


def test_a_series_shorter_than_the_ceiling_is_a_measurement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The other half: the flag must be able to be false while the same search runs.

    Without this, a report that hard-coded `True` would pass the test above — and every count in
    the product would carry a warning saying it cannot be trusted.
    """
    monkeypatch.setattr(mt5_source, "_POSITION_CEILING", 8)
    series = a_series(filler_years=0, real_years=2, stamped_years=0)[:5]

    with MT5Source(terminal=_SeriesTerminal(series), server_offset=SERVER_OFFSET) as source:
        report = source.probe_history("EURUSD", "D1")

    assert report.bar_count_is_a_ceiling is False
    assert report.bar_count == len(series)


def test_a_fixed_spread_instrument_gets_no_cost_floor() -> None:
    """⚠️ The false positive that would land on the most trustworthy data in the catalogue.

    `spread_float` false means the broker really does charge the same spread always — this
    project's previous broker quoted AAPL that way. Every year is constant there, and reading
    that as invented costs would warn about the one instrument whose costs are certain.
    """

    class _Fixed(_SeriesTerminal):
        def symbol_info(self, symbol: str) -> _SymbolInfo | None:
            return _SymbolInfo(name=symbol, spread_float=False)

    terminal = _Fixed(a_series(filler_years=0, real_years=3, stamped_years=0))

    with MT5Source(terminal=terminal, server_offset=SERVER_OFFSET) as source:
        report = source.probe_history("EURUSD", "D1")

    assert report.first_measured_cost is None

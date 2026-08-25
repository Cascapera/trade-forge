"""Differencing the broker's ledger, without a database.

The broker reports **what is true now**; a row has to be written for **what changed**. Every
bug this module can have lives in that gap, and none of it needs Postgres: a trade that opens
and closes inside one bar, a close that must find its own open row, a bar that changed nothing.

`test_recorder_integration.py` proves the three statements those answers turn into.
"""

import datetime as dt
from collections.abc import Sequence
from decimal import Decimal

from tradeforge_api.live.recorder import LedgerView, LedgerWatch
from tradeforge_engine.backtest_broker import BacktestBroker
from tradeforge_engine.domain import ClosedTrade, Position, Side
from tradeforge_engine.testing import EURUSD

SYMBOL = "EURUSD"
START = dt.datetime(2026, 8, 25, 12, tzinfo=dt.UTC)
HOUR = dt.timedelta(hours=1)


def a_position(index: int) -> Position:
    return Position(
        symbol=SYMBOL,
        side=Side.LONG,
        volume=Decimal("0.10"),
        entry_price=Decimal("1.10000"),
        entry_time=START + index * HOUR,
        initial_stop_loss=Decimal("1.09500"),
        stop_loss=Decimal("1.09500"),
    )


def a_trade(index: int) -> ClosedTrade:
    """The round trip that `a_position(index)` becomes when it closes."""
    return ClosedTrade(
        symbol=SYMBOL,
        side=Side.LONG,
        volume=Decimal("0.10"),
        entry_time=START + index * HOUR,
        entry_price=Decimal("1.10000"),
        exit_time=START + (index + 2) * HOUR,
        exit_price=Decimal("1.10500"),
        gross_pnl=Decimal("50"),
        costs=Decimal("2"),
        net_pnl=Decimal("48"),
        reason="tp",
        stop_loss=Decimal("1.09500"),
    )


class FakeBroker:
    """Only the two questions `LedgerWatch` asks. Set them, then step."""

    def __init__(self) -> None:
        self.open: list[Position] = []
        self.closed: list[ClosedTrade] = []

    def positions(self, symbol: str) -> Sequence[Position]:
        return [position for position in self.open if position.symbol == symbol]

    def trades(self) -> Sequence[ClosedTrade]:
        return self.closed


def entries(changes: object, attribute: str) -> list[int]:
    """A bar's trades as their index, so a scenario reads as `[0, 1]`."""
    return [round((item.entry_time - START) / HOUR) for item in getattr(changes, attribute)]


def test_a_quiet_bar_earns_no_writes() -> None:
    """The overwhelming majority of bars. A recorder that wrote on every bar would grow the
    table without anything having happened, and `__bool__` is what the caller skips on."""
    watch = LedgerWatch(SYMBOL)

    changes = watch.step(FakeBroker())

    assert not changes
    assert changes.to_open is None
    assert changes.to_close == ()
    assert changes.to_insert_closed == ()


def test_a_position_that_appears_is_reported_once() -> None:
    """⚠️ **Once.** The broker keeps answering "I hold this" for every bar the trade is open —
    that is what `positions()` means — so a watch that reported it each time would insert a
    duplicate row on the second bar and hit the partial unique index on the third."""
    broker = FakeBroker()
    watch = LedgerWatch(SYMBOL)
    broker.open = [a_position(0)]

    first = watch.step(broker)
    second = watch.step(broker)
    third = watch.step(broker)

    assert first.to_open == a_position(0)
    assert second.to_open is None, "the same position was offered twice"
    assert third.to_open is None


def test_a_trade_that_closes_updates_the_row_it_already_has() -> None:
    broker = FakeBroker()
    watch = LedgerWatch(SYMBOL)
    broker.open = [a_position(0)]
    watch.step(broker)

    broker.open = []
    broker.closed = [a_trade(0)]
    changes = watch.step(broker)

    assert entries(changes, "to_close") == [0]
    assert changes.to_insert_closed == (), "a second row would be inserted"
    assert changes.to_open is None


def test_a_trade_that_opens_and_closes_inside_one_bar_is_a_single_finished_row() -> None:
    """⚠️ The case a two-step design forgets. A stop hit on the same candle that filled the
    entry never leaves a moment when anyone could have seen the position open — so there is no
    open row, and the close has nothing to update. Handled as an UPDATE it would match zero rows
    and the session would raise on its first scalped trade.
    """
    broker = FakeBroker()
    watch = LedgerWatch(SYMBOL)

    broker.closed = [a_trade(0)]
    changes = watch.step(broker)

    assert entries(changes, "to_insert_closed") == [0]
    assert changes.to_close == (), "it tried to update a row that was never written"


def test_a_stop_and_a_reversal_on_one_bar_are_both_recorded() -> None:
    """⚠️ The scenario that makes the ordering inside `step` load-bearing. One trade ends and
    another begins on the same candle: the close must be worked out against the row that exists,
    and the new position must still be offered as an open. A watch that read the position first
    would attribute the new entry to the row the closing trade is about to claim — not a crash,
    just one trade recorded with another's entry.
    """
    broker = FakeBroker()
    watch = LedgerWatch(SYMBOL)
    broker.open = [a_position(0)]
    watch.step(broker)

    broker.open = [a_position(1)]
    broker.closed = [a_trade(0)]
    changes = watch.step(broker)

    assert entries(changes, "to_close") == [0]
    assert changes.to_open is not None
    assert changes.to_open.entry_time == a_position(1).entry_time


def test_two_round_trips_settling_on_the_same_bar_are_both_reported() -> None:
    """⚠️ A loop with one item to walk is a loop with no proof. `trades()` grows by more than
    one whenever a bar closes a position and a scalp inside the same candle."""
    broker = FakeBroker()
    watch = LedgerWatch(SYMBOL)
    broker.open = [a_position(0)]
    watch.step(broker)

    broker.open = []
    broker.closed = [a_trade(0), a_trade(5)]
    changes = watch.step(broker)

    assert entries(changes, "to_close") == [0], "the one with a row"
    assert entries(changes, "to_insert_closed") == [5], "the one without"


def test_a_settled_trade_is_never_reported_twice() -> None:
    """`trades()` is the run's whole history, not a queue — it keeps answering with everything
    it has ever closed. A watch that re-read it would rewrite every exit on every bar."""
    broker = FakeBroker()
    watch = LedgerWatch(SYMBOL)
    broker.open = [a_position(0)]
    watch.step(broker)
    broker.open = []
    broker.closed = [a_trade(0)]
    watch.step(broker)

    again = watch.step(broker)

    assert not again


def test_another_symbol_s_position_is_not_this_session_s() -> None:
    """⚠️ A live broker's account holds more than this session's symbol. Recording another
    instrument's position against this session would attach a trade to the wrong instrument
    row, and the P&L would still add up — which is what makes it hard to notice."""
    broker = FakeBroker()
    watch = LedgerWatch(SYMBOL)
    other = Position(
        symbol="GBPUSD",
        side=Side.LONG,
        volume=Decimal("0.10"),
        entry_price=Decimal("1.30000"),
        entry_time=START,
        initial_stop_loss=Decimal("1.29500"),
        stop_loss=Decimal("1.29500"),
    )
    broker.open = [other]

    assert watch.step(broker).to_open is None


def test_the_position_offered_is_the_one_the_broker_holds() -> None:
    """Not a copy this module built from a fill. `positions()` is the venue's own answer to
    "what am I holding", which is the question the row exists to record."""
    broker = FakeBroker()
    watch = LedgerWatch(SYMBOL)
    held = a_position(3)
    broker.open = [held]

    assert watch.step(broker).to_open is held


def test_a_reopened_entry_instant_is_offered_again() -> None:
    """⚠️ The mark is cleared when the trade closes, not kept for ever. Two sessions of a
    strategy that enters on the same clock instant on different days would otherwise have the
    second entry silently skipped — and `entry_time` carries the date, so this is really about
    a watch that never forgets growing without bound."""
    broker = FakeBroker()
    watch = LedgerWatch(SYMBOL)
    broker.open = [a_position(0)]
    watch.step(broker)
    broker.open = []
    broker.closed = [a_trade(0)]
    watch.step(broker)

    broker.open = [a_position(0)]
    changes = watch.step(broker)

    assert changes.to_open is not None, "the instant stayed marked after the trade closed"


def test_changes_are_falsy_only_when_there_is_nothing_to_write() -> None:
    """`__bool__` is what the caller skips the write on, so it has to be exactly right — a
    version that only looked at `to_open` would drop every close."""
    broker = FakeBroker()
    watch = LedgerWatch(SYMBOL)

    broker.closed = [a_trade(0)]
    assert bool(watch.step(broker)) is True, "a close alone must count"

    broker.open = [a_position(1)]
    assert bool(watch.step(broker)) is True, "an open alone must count"

    assert bool(watch.step(broker)) is False


def test_the_real_broker_satisfies_the_ledger_view() -> None:
    """⚠️ Proved by assignment, so mypy checks it. A `Protocol` nothing is ever assigned to
    describes an imaginary client — this repository has already shipped one the real redis
    client did not satisfy, with every test green.

    And the narrowing is the point of the protocol: the first draft asked for the whole `Broker`,
    which nothing here needs, and the cost landed immediately on the fake above — it could not
    answer "what are you holding" without also pretending to be a venue.
    """
    broker = BacktestBroker(instrument=EURUSD, initial_capital=Decimal("10000"))

    view: LedgerView = broker

    # ⚠️ Emptiness, not `== []`. The real broker answers with tuples and the fake above with
    # lists; the protocol says `Sequence`, and both are. A test that pinned the concrete type
    # would be asserting something the caller neither needs nor gets.
    assert list(view.trades()) == []
    assert list(view.positions(EURUSD.symbol)) == []

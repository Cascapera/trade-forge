"""The event loop: what runs before what, and what that makes impossible.

Every test here is about **order of operations**. There is no fill model yet (PR-105) and
no indicator (PR-104); either would only obscure the question this PR exists to settle:
can a decision made on candle N ever be executed with information from candle N?
"""

import datetime as dt
from decimal import Decimal

import pytest

from tradeforge_engine.domain import Candle, OrderRequest, OrderResult, Position, Side, SignalKind
from tradeforge_engine.errors import EngineError, LookaheadError
from tradeforge_engine.loop import run
from tradeforge_engine.testing import (
    EURUSD,
    HOUR,
    FixedRisk,
    ImmediateFillBroker,
    ScriptedStrategy,
    close_out,
    entry,
    rising,
)


def test_a_decision_on_one_candle_is_filled_at_the_open_of_the_next() -> None:
    """The central invariant, as plainly as it can be stated.

    The strategy decides on the close of candle 2. The fill happens at the open of candle
    3 — a price that did not exist anywhere at the moment of the decision.
    """
    candles = rising(6)

    result = run(
        timeframe=HOUR,
        candles=candles,
        instrument=EURUSD,
        strategy=ScriptedStrategy(script={2: [entry()]}),
        broker=ImmediateFillBroker(),
        risk=FixedRisk(),
    )

    [fill] = result.fills
    assert fill.order.decided_at == candles[2].time
    assert fill.time == candles[3].time
    assert fill.price == candles[3].open


def test_the_bars_fills_are_shown_to_the_strategy_that_bar() -> None:
    """A strategy learns its order became a trade from the bar that filled it (ADR-0015).

    The fill is born in step 1 and the strategy runs in step 2 of the same iteration, so the
    hand-off is bar-N information reaching a bar-N decision — the live terminal's fill
    notification, replayed. `position` alone cannot carry this: a position opened and stopped
    out inside one bar is gone before the strategy runs, and a strategy never told about the
    fill would treat its order as still resting.
    """
    candles = rising(6)
    strategy = ScriptedStrategy(script={2: [entry()]})

    result = run(
        timeframe=HOUR,
        candles=candles,
        instrument=EURUSD,
        strategy=strategy,
        broker=ImmediateFillBroker(),
        risk=FixedRisk(),
    )

    [fill] = result.fills
    assert strategy.fills_seen[3] == (fill,)  # shown on the bar the fill was born in
    assert all(seen == () for index, seen in enumerate(strategy.fills_seen) if index != 3)


def test_the_strategy_is_handed_one_candle_and_never_the_series() -> None:
    """Structural, not conventional: there is no future in the object to peek at."""
    candles = rising(4)
    strategy = ScriptedStrategy()

    run(
        timeframe=HOUR,
        candles=candles,
        instrument=EURUSD,
        strategy=strategy,
        broker=ImmediateFillBroker(),
        risk=FixedRisk(),
    )

    assert strategy.seen == candles


def test_an_entry_is_sized_by_the_risk_manager_and_not_by_the_strategy() -> None:
    """Sizing is plugged in, so the same strategy runs on a $1 000 and a $1 000 000 account."""
    risk = FixedRisk(volume=Decimal("0.25"))

    result = run(
        timeframe=HOUR,
        candles=rising(5),
        instrument=EURUSD,
        strategy=ScriptedStrategy(script={1: [entry()]}),
        broker=ImmediateFillBroker(),
        risk=risk,
    )

    [fill] = result.fills
    assert fill.volume == Decimal("0.25")
    assert len(risk.sized) == 1


def test_a_size_of_zero_means_no_trade() -> None:
    """The risk manager's way of declining without raising."""
    result = run(
        timeframe=HOUR,
        candles=rising(5),
        instrument=EURUSD,
        strategy=ScriptedStrategy(script={1: [entry()]}),
        broker=ImmediateFillBroker(),
        risk=FixedRisk(volume=Decimal(0)),
    )

    assert result.fills == ()


def test_a_vetoed_order_never_reaches_the_broker() -> None:
    """The kill switch and the daily loss limit live here, not in the sizing arithmetic.

    Separate questions, separate methods: a bug in "how big" must not become a bug in
    "should we be trading at all".
    """
    broker = ImmediateFillBroker()
    risk = FixedRisk(allow_all=False)

    result = run(
        timeframe=HOUR,
        candles=rising(5),
        instrument=EURUSD,
        strategy=ScriptedStrategy(script={1: [entry()]}),
        broker=broker,
        risk=risk,
    )

    assert broker.submitted == []
    assert result.fills == ()
    assert len(risk.vetoed) == 1


def test_an_exit_closes_the_whole_position_and_skips_the_risk_manager() -> None:
    """Sizing an exit is how a stop-loss gets rejected for exceeding the daily loss limit.

    The position closing *is* the thing that stops the loss. It is not new risk to budget.
    """
    risk = FixedRisk(volume=Decimal("0.75"))

    result = run(
        timeframe=HOUR,
        candles=rising(8),
        instrument=EURUSD,
        strategy=ScriptedStrategy(script={1: [entry()], 4: [close_out()]}),
        broker=ImmediateFillBroker(),
        risk=risk,
    )

    open_fill, close_fill = result.fills
    assert open_fill.order.intent is SignalKind.ENTRY
    assert close_fill.order.intent is SignalKind.EXIT
    assert close_fill.volume == open_fill.volume == Decimal("0.75")
    # Sized once — for the entry. The exit never asked.
    assert len(risk.sized) == 1


def test_an_exit_with_nothing_open_is_ignored_rather_than_fatal() -> None:
    """A strategy may emit an exit on a bar where its position was already stopped out."""
    result = run(
        timeframe=HOUR,
        candles=rising(5),
        instrument=EURUSD,
        strategy=ScriptedStrategy(script={1: [close_out()]}),
        broker=ImmediateFillBroker(),
        risk=FixedRisk(),
    )

    assert result.fills == ()


def test_the_strategy_sees_its_position_only_once_the_fill_has_happened() -> None:
    """Which is the bar *after* the decision — and it is what exit rules are decided on."""
    strategy = ScriptedStrategy(script={1: [entry(Side.LONG)]})

    run(
        timeframe=HOUR,
        candles=rising(6),
        instrument=EURUSD,
        strategy=strategy,
        broker=ImmediateFillBroker(),
        risk=FixedRisk(),
    )

    # Bars 0 and 1: flat. Decided on bar 1, filled at the open of bar 2 — so the position
    # first shows up in the context on bar 2.
    assert strategy.positions_seen == [None, None, Side.LONG, Side.LONG, Side.LONG, Side.LONG]


def test_the_equity_curve_has_one_point_per_candle() -> None:
    candles = rising(10)

    result = run(
        timeframe=HOUR,
        candles=candles,
        instrument=EURUSD,
        strategy=ScriptedStrategy(),
        broker=ImmediateFillBroker(),
        risk=FixedRisk(),
    )

    assert result.candles_processed == 10
    assert [point.time for point in result.equity_curve] == [c.time for c in candles]


def test_candles_going_backwards_are_refused() -> None:
    """A replayed bar lets a strategy act twice on the same information.

    That is lookahead wearing a different hat, so it raises the same error.
    """
    candles = rising(4)
    scrambled = [candles[0], candles[2], candles[1], candles[3]]

    with pytest.raises(LookaheadError, match="strictly increasing"):
        run(
            timeframe=HOUR,
            candles=scrambled,
            instrument=EURUSD,
            strategy=ScriptedStrategy(),
            broker=ImmediateFillBroker(),
            risk=FixedRisk(),
        )


def test_a_duplicated_candle_is_refused() -> None:
    candles = rising(3)

    with pytest.raises(LookaheadError, match="strictly increasing"):
        run(
            timeframe=HOUR,
            candles=[*candles, candles[-1]],
            instrument=EURUSD,
            strategy=ScriptedStrategy(),
            broker=ImmediateFillBroker(),
            risk=FixedRisk(),
        )


def test_an_empty_stream_is_an_empty_run_not_a_crash() -> None:
    result = run(
        timeframe=HOUR,
        candles=[],
        instrument=EURUSD,
        strategy=ScriptedStrategy(),
        broker=ImmediateFillBroker(),
        risk=FixedRisk(),
    )

    assert result.candles_processed == 0
    assert result.equity_curve == ()
    assert result.final_account.equity == Decimal(10_000)


def test_the_engine_accepts_a_generator_and_never_holds_the_series() -> None:
    """Ten years of M1 is five million bars. The engine has no reason to keep them all."""
    result = run(
        timeframe=HOUR,
        candles=(candle for candle in rising(5)),
        instrument=EURUSD,
        strategy=ScriptedStrategy(),
        broker=ImmediateFillBroker(),
        risk=FixedRisk(),
    )

    assert result.candles_processed == 5


def test_an_order_decided_on_the_final_candle_never_executes() -> None:
    """There is no next bar to fill it at, so it does not fill. Correct, and worth stating.

    The alternative — filling it on the candle it was decided on — is the exact lookahead
    this design exists to prevent.
    """
    result = run(
        timeframe=HOUR,
        candles=rising(3),
        instrument=EURUSD,
        strategy=ScriptedStrategy(script={2: [entry()]}),
        broker=ImmediateFillBroker(),
        risk=FixedRisk(),
    )

    assert result.fills == ()


def test_a_weekend_gap_fills_at_the_monday_open() -> None:
    """Which is what a real broker does — and where a naive engine would use Friday's close."""
    friday = Candle(
        time=dt.datetime(2024, 1, 5, 21, tzinfo=dt.UTC),
        open=Decimal("1.10000"),
        high=Decimal("1.10100"),
        low=Decimal("1.09900"),
        close=Decimal("1.10050"),
    )
    monday = Candle(
        time=dt.datetime(2024, 1, 8, 0, tzinfo=dt.UTC),
        open=Decimal("1.10200"),
        high=Decimal("1.10300"),
        low=Decimal("1.10100"),
        close=Decimal("1.10250"),
    )

    result = run(
        timeframe=HOUR,
        candles=[friday, monday],
        instrument=EURUSD,
        strategy=ScriptedStrategy(script={0: [entry()]}),
        broker=ImmediateFillBroker(),
        risk=FixedRisk(),
    )

    [fill] = result.fills
    assert fill.time == monday.time
    assert fill.price == monday.open


def test_two_open_positions_in_the_traded_symbol_are_refused() -> None:
    """Phase 1 holds one position at a time, and the engine will not quietly pick one.

    If a broker reports two open positions in the symbol being traded, `_open_position` has
    no honest answer to "what is my position?" — building an exit out of either one throws
    away the other. It raises rather than guess, because guessing is how a strategy ends up
    closing half of a book it did not know it had.
    """

    def a_position(price: str) -> Position:
        return Position(
            symbol="EURUSD",
            side=Side.LONG,
            volume=Decimal(1),
            entry_price=Decimal(price),
            entry_time=dt.datetime(2024, 1, 1, tzinfo=dt.UTC),
        )

    class AccountWithTwoEurusdPositions(ImmediateFillBroker):
        def positions(self, symbol: str) -> list[Position]:
            return [a_position("1.10000"), a_position("1.10100")]

    with pytest.raises(EngineError, match="phase 1 holds one at a time"):
        run(
            timeframe=HOUR,
            candles=rising(4),
            instrument=EURUSD,
            strategy=ScriptedStrategy(),
            broker=AccountWithTwoEurusdPositions(),
            risk=FixedRisk(),
        )


def test_the_broker_is_reached_only_through_the_protocol() -> None:
    """Nothing in `testing.py` inherits from the engine. Structural typing, not a base class."""
    broker = ImmediateFillBroker()

    assert type(broker).__mro__[1:] == (object,)
    assert isinstance(broker.submit(_an_order()), OrderResult)


def _an_order() -> OrderRequest:
    return OrderRequest(
        symbol="EURUSD",
        side=Side.LONG,
        intent=SignalKind.ENTRY,
        volume=Decimal(1),
        decided_at=dt.datetime(2024, 1, 1, tzinfo=dt.UTC),
    )

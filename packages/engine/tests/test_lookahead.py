"""The lookahead guard, attacked from every angle a broker can get it wrong.

These are the tests the engine exists for. Each one models a real bug — one that ships, one
that produces a beautiful equity curve, and one that nobody goes looking for precisely
because the numbers look good.

Note that a 100%-covered loop passed every one of these *before* the guard was fixed. Line
coverage tells you a line ran; it says nothing about whether the condition on it was right.
"""

import datetime as dt
from collections.abc import Sequence
from decimal import Decimal

import pytest

from tradeforge_engine.domain import Candle, Fill, OrderRequest, Position, Side, SignalKind
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


def a_run(broker: ImmediateFillBroker) -> object:
    return run(
        candles=rising(6),
        timeframe=HOUR,
        instrument=EURUSD,
        strategy=ScriptedStrategy(script={1: [entry()]}),
        broker=broker,
        risk=FixedRisk(),
    )


class FillsAtTheDecisionClose(ImmediateFillBroker):
    """Fills half an hour into the very candle the strategy decided on, at its close.

    This is the classic: "decide on the breakout, execute on the breakout". The price used
    is one the strategy had already read when it made the call. The old guard —
    `fill.time <= decided_at` — let this straight through, because `decided_at` is the
    candle's *opening* instant and this fill is stamped thirty minutes later.
    """

    def on_bar(self, candle: Candle) -> Sequence[Fill]:
        fills = [
            Fill(
                order=order,
                time=order.decided_at + dt.timedelta(minutes=30),
                price=candle.close,
                volume=order.volume,
                costs=Decimal(0),
            )
            for order in self._pending
        ]
        self._pending.clear()
        return fills


class FillsFromTheFuture(ImmediateFillBroker):
    """Off by one in the index: `candles[i+1].open` instead of `candles[i].open`.

    The most common bug there is in a backtest broker. It fills at a price that has not
    happened yet, and the equity curve does not look wrong — it looks *excellent*.
    """

    def on_bar(self, candle: Candle) -> Sequence[Fill]:
        fills = [
            Fill(
                order=order,
                time=candle.time + dt.timedelta(days=7),
                price=candle.open,
                volume=order.volume,
                costs=Decimal(0),
            )
            for order in self._pending
        ]
        self._pending.clear()
        return fills


class FillsExactlyAtTheDecision(ImmediateFillBroker):
    """Stamps the fill at the decision instant itself — the boundary case."""

    def on_bar(self, candle: Candle) -> Sequence[Fill]:  # noqa: ARG002
        fills = [
            Fill(
                order=order,
                time=order.decided_at,
                price=Decimal("1.10000"),
                volume=order.volume,
                costs=Decimal(0),
            )
            for order in self._pending
        ]
        self._pending.clear()
        return fills


class FillsAtAPriceTheBarNeverTraded(ImmediateFillBroker):
    """Books the fill at a price outside the bar's own range.

    The most expensive fantasy in backtesting: "the stop always fills at the stop level".
    A long position has its stop above where the bar opens; price gaps up through it, and
    the bar's whole range sits below the stop. A naive broker reports the fill *at the stop*
    anyway — a price nobody in the market ever paid — and the backtest books a gain the tape
    would never have handed you. The fill's time is honest (inside the bar) and its
    `decided_at` is honest (an earlier bar), so it clears both the floor and the ceiling.
    Only the range guard sees it.
    """

    def on_bar(self, candle: Candle) -> Sequence[Fill]:
        fills = [
            Fill(
                order=order,
                time=candle.time,
                price=candle.high + Decimal("0.00500"),  # above everything the bar traded at
                volume=order.volume,
                costs=Decimal(0),
            )
            for order in self._pending
        ]
        self._pending.clear()
        return fills


def test_a_fill_at_a_price_the_bar_never_traded_is_refused() -> None:
    """The range guard, and the only test that reaches it.

    The floor and the ceiling both pass — the fill is stamped inside the bar being processed
    and the order was decided on an earlier one. Every other lookahead test fills at
    `candle.open` or `candle.close`, always within `[low, high]`, so the range check has
    never once fired. Line coverage said it ran; it never ran *true*.
    """
    with pytest.raises(LookaheadError, match="nobody traded there"):
        a_run(FillsAtAPriceTheBarNeverTraded())


def test_a_fill_inside_the_decision_candle_is_refused() -> None:
    """A price the strategy had already read is not a price it can trade at.

    Caught by the ceiling: the fill is stamped inside candle N, but the bar being processed
    is N+1. The old guard — `fill.time <= decided_at` — waved this through, because
    `decided_at` is candle N's *opening* instant and this fill lands half an hour later.
    """
    with pytest.raises(LookaheadError, match="outside the candle being processed"):
        a_run(FillsAtTheDecisionClose())


def test_a_fill_at_the_exact_decision_instant_is_refused() -> None:
    with pytest.raises(LookaheadError, match="outside the candle being processed"):
        a_run(FillsExactlyAtTheDecision())


def test_a_broker_that_backdates_its_own_order_to_the_current_bar_is_refused() -> None:
    """The floor, and the reason it exists.

    A broker that synthesises a protective exit and stamps it with the *current* candle's
    time is claiming the decision was made on the bar it is filling. That is a decision
    taken with the bar's own data — and this is the only guard that sees it, because the
    fill itself lands perfectly inside the bar and sails past the ceiling.
    """

    class BackdatesTheStop(ImmediateFillBroker):
        def on_bar(self, candle: Candle) -> Sequence[Fill]:
            fills: list[Fill] = []
            for order in self._pending:
                forged = OrderRequest(
                    symbol=order.symbol,
                    side=order.side,
                    intent=order.intent,
                    volume=order.volume,
                    decided_at=candle.time,  # <- claims it decided on the bar it is filling
                )
                fills.append(
                    Fill(
                        order=forged,
                        time=candle.time,
                        price=candle.open,
                        volume=order.volume,
                        costs=Decimal(0),
                    )
                )
            self._pending.clear()
            return fills

    with pytest.raises(LookaheadError, match="never within the candle it was taken from"):
        a_run(BackdatesTheStop())


def test_a_fill_from_beyond_the_current_bar_is_refused() -> None:
    """The ceiling. Without it, an off-by-one broker reads prices that have not happened."""
    with pytest.raises(LookaheadError, match="outside the candle being processed"):
        a_run(FillsFromTheFuture())


def test_the_honest_broker_passes_both_checks() -> None:
    """And the guard is not so tight that a correct broker trips it."""
    result = run(
        candles=rising(6),
        timeframe=HOUR,
        instrument=EURUSD,
        strategy=ScriptedStrategy(script={1: [entry()]}),
        broker=ImmediateFillBroker(),
        risk=FixedRisk(),
    )

    [fill] = result.fills
    assert fill.time == rising(6)[2].time


def test_a_protective_exit_may_fill_on_the_bar_its_entry_opened() -> None:
    """A stop hit intrabar, on the very candle the position opened. Legitimate — and the
    guard must not reject it.

    The exit order inherits the entry's `decided_at`: the stop level was decided when the
    stop was set, not when price reached it. That is what makes this pass, and it is the
    contract PR-105's broker will rely on.
    """

    class StopsOutOnEntryBar(ImmediateFillBroker):
        def on_bar(self, candle: Candle) -> Sequence[Fill]:
            fills: list[Fill] = []
            for order in self._pending:
                fills.append(
                    Fill(
                        order=order,
                        time=candle.time,
                        price=candle.open,
                        volume=order.volume,
                        costs=Decimal(0),
                    )
                )
                # The stop, hit inside the same bar. Same `decided_at` as the entry.
                stop_exit = OrderRequest(
                    symbol=order.symbol,
                    side=order.side,
                    intent=SignalKind.EXIT,
                    volume=order.volume,
                    decided_at=order.decided_at,
                    reason="sl",
                )
                fills.append(
                    Fill(
                        order=stop_exit,
                        time=candle.time + dt.timedelta(minutes=45),
                        price=candle.low,
                        volume=order.volume,
                        costs=Decimal(0),
                    )
                )
            self._pending.clear()

            for fill in fills:
                self._portfolio.apply(fill)
            self._portfolio.mark_to_market(candle)
            return fills

    result = run(
        candles=rising(5),
        timeframe=HOUR,
        instrument=EURUSD,
        strategy=ScriptedStrategy(script={1: [entry()]}),
        broker=StopsOutOnEntryBar(),
        risk=FixedRisk(),
    )

    assert len(result.fills) == 2
    assert len(result.trades) == 1
    assert result.trades[0].reason == "sl"


def test_a_fill_for_another_symbol_is_refused() -> None:
    """This run's ledger is not the place for another instrument's trade."""

    class FillsGold(ImmediateFillBroker):
        def on_bar(self, candle: Candle) -> Sequence[Fill]:
            fills = [
                Fill(
                    order=OrderRequest(
                        symbol="XAUUSD",
                        side=order.side,
                        intent=order.intent,
                        volume=order.volume,
                        decided_at=order.decided_at,
                    ),
                    time=candle.time,
                    price=candle.open,
                    volume=order.volume,
                    costs=Decimal(0),
                )
                for order in self._pending
            ]
            self._pending.clear()
            return fills

    with pytest.raises(EngineError, match="fill for XAUUSD while running EURUSD"):
        a_run(FillsGold())


def test_a_position_in_another_symbol_never_becomes_an_exit_order() -> None:
    """A live MT5 account holds trades this strategy never opened.

    Taking `positions()[0]` would build an exit out of somebody else's gold short — with its
    side and its volume — and send it as an order to close that many lots of EURUSD. To a
    real broker. The protocol takes a symbol precisely so this cannot be expressed.
    """

    gold_short = Position(
        symbol="XAUUSD",
        side=Side.SHORT,
        volume=Decimal(5),
        entry_price=Decimal("2000.00"),
        entry_time=dt.datetime(2024, 1, 1, tzinfo=dt.UTC),
    )

    class AccountWithGold(ImmediateFillBroker):
        def positions(self, symbol: str) -> Sequence[Position]:
            # What a real terminal reports: everything the account holds. The filtering is
            # the broker's job precisely *because* the engine asked for a symbol.
            everything = [gold_short, *super().positions(symbol)]
            return [position for position in everything if position.symbol == symbol]

    result = run(
        candles=rising(6),
        timeframe=HOUR,
        instrument=EURUSD,
        strategy=ScriptedStrategy(script={1: [entry()], 3: [close_out()]}),
        broker=AccountWithGold(),
        risk=FixedRisk(),
    )

    # The exit closed the EURUSD position, at its volume — not five lots of gold.
    exit_fills = [fill for fill in result.fills if fill.order.intent is SignalKind.EXIT]
    assert len(exit_fills) == 1
    assert exit_fills[0].order.symbol == "EURUSD"
    assert exit_fills[0].volume == Decimal(1)

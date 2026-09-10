"""The published 9.1: the bar that bent the MME9 is the trade, and the order waits where it fell.

Two setups in this engine are called 9.1, and most of this file exists to hold them apart. The
author's (`Mme9BreakoutStrategy`) arms on a bar closing across the average, re-prices to the newest
bar of the turn, and is conducted by the average. The literature's (`Mme9TurnStrategy`) arms on the
average's slope changing sign, leaves the order at the bar that bent the line, and is conducted by
nothing unless asked. ⚠️ On an exponential average **the arming events coincide** — see
`test_slope_and_close_are_the_same_event_on_an_ema` — so a test that only checks where the first
order lands does not separate the two classes at all. What separates them is the bars *after* the
turn, and those tests run the sibling over the same candles to show it answering differently.

Golden numbers come from feeding these exact bars to the engine and reading what came out, never
from arithmetic done here.
"""

from decimal import Decimal, localcontext

from hypothesis import given, settings
from hypothesis import strategies as st

from tradeforge_engine.backtest_broker import BacktestBroker
from tradeforge_engine.domain import (
    Candle,
    Context,
    Fill,
    OrderRequest,
    Position,
    Side,
    Signal,
    SignalKind,
)
from tradeforge_engine.indicators import EMA
from tradeforge_engine.loop import ENGINE_CONTEXT, RunResult, run
from tradeforge_engine.risk import PercentRiskManager
from tradeforge_engine.swing import Mme9BreakoutStrategy, Mme9TurnStrategy
from tradeforge_engine.testing import AAPL, HOUR, ImmediateFillBroker, bar

_ACCOUNT = ImmediateFillBroker(instrument=AAPL).account()

# A falling MME3, then the bar that bends it up, then two more bars on the same side. The closes
# are what matter; each bar carries a fixed body and wick around its close so the high and the low
# are read straight off it.
_TURN_UP = ["100", "99", "98", "97", "96", "95", "105", "106", "105"]

# The same turn, followed by a bar that runs and one that closes back under the average — which is
# the event the author's setup conducts by and this one ignores. Kept apart from `_TURN_UP` because
# a series that both keeps rising and closes back cannot exist.
_CLOSES_BACK = ["100", "99", "98", "97", "96", "95", "105", "112", "105.5"]


def _candles(closes: list[str]) -> list[Candle]:
    return [
        bar(
            index,
            open_=str(float(c) - 0.5),
            close=c,
            high=str(float(c) + 1),
            low=str(float(c) - 1.5),
        )
        for index, c in enumerate(closes)
    ]


def _drive(
    strategy: Mme9TurnStrategy | Mme9BreakoutStrategy,
    candles: list[Candle],
    *,
    position_on: frozenset[int] = frozenset(),
    held: Position | None = None,
) -> list[list[Signal]]:
    """Feed candles one at a time and collect what each bar produced, including the silences.

    A resting order that does not move emits nothing, and "nothing" is this setup's whole claim
    against its sibling — so the bars that say nothing have to be visible, which the loop's
    aggregate result cannot show.
    """
    out: list[list[Signal]] = []
    with localcontext(ENGINE_CONTEXT):
        for index, candle in enumerate(candles):
            position = held if index in position_on else None
            context = Context(
                candle=candle,
                instrument=AAPL,
                account=_ACCOUNT,
                position=position,
                fills=(),
            )
            out.append(list(strategy.on_bar(context)))
    return out


def _run(candles: list[Candle], *, side: Side = Side.LONG, period: int = 3) -> RunResult:
    return run(
        candles=candles,
        timeframe=HOUR,
        instrument=AAPL,
        strategy=Mme9TurnStrategy(side=side, period=period),
        broker=BacktestBroker(instrument=AAPL, initial_capital=Decimal(100_000)),
        risk=PercentRiskManager(percent=Decimal("1")),
    )


def _kinds(signals: list[list[Signal]]) -> list[list[str]]:
    return [[signal.kind.name for signal in bars] for bars in signals]


def test_the_bar_that_bends_the_average_arms_at_its_high_and_is_protected_at_its_low() -> None:
    signals = _drive(Mme9TurnStrategy(side=Side.LONG, period=3), _candles(_TURN_UP))

    (entry,) = signals[6]
    assert entry.kind is SignalKind.ENTRY
    assert entry.side is Side.LONG
    # The bar closed at 105, so its high is 106 and its low 103.5 — the whole bar is the trade.
    assert str(entry.stop_price) == "106.0"
    assert str(entry.stop_loss) == "103.50"
    assert entry.reason == "entry.mme9turn"
    # The average *this* bar produced, not the one it bent away from — 96.000 is the reading
    # before it, and carrying that would put a plausible wrong number on every entry record.
    assert entry.context == {"average": Decimal("100.5")}
    # And the same average as the curve that gets drawn. A scalar cannot show a bend.
    (curve,) = entry.series
    assert curve.points[-1].value == entry.context["average"]


def test_the_sell_side_is_the_mirror() -> None:
    # A rising MME3 bent down by one bar closing at 95: the order rests at that bar's low and is
    # protected at its high, and the line turning back up withdraws it.
    signals = _drive(
        Mme9TurnStrategy(side=Side.SHORT, period=3),
        _candles(["100", "101", "102", "103", "104", "105", "95", "94", "96", "108"]),
    )

    (entry,) = signals[6]
    assert entry.kind is SignalKind.ENTRY
    assert entry.side is Side.SHORT
    assert str(entry.stop_price) == "93.5"
    assert str(entry.stop_loss) == "96.00"
    assert _kinds(signals)[7:] == [[], [], ["CANCEL"]]


def test_the_order_does_not_follow_price_while_the_line_keeps_pointing_up() -> None:
    """The reference is frozen: bars 7 and 8 stay on the setup's side and change nothing.

    ⚠️ **This is the test that separates the two 9.1s**, so it also runs the author's over the
    same bars: his re-prices to the newest bar — *"vale sempre a última barra"* — and answers
    those same two bars with a cancel and a fresh entry each time.
    """
    published = _drive(Mme9TurnStrategy(side=Side.LONG, period=3), _candles(_TURN_UP))
    assert _kinds(published)[6:] == [["ENTRY"], [], []]

    his = _drive(Mme9BreakoutStrategy(side=Side.LONG, period=3), _candles(_TURN_UP))
    assert _kinds(his)[6:] == [["ENTRY"], ["CANCEL", "ENTRY"], ["CANCEL", "ENTRY"]]
    # And his trigger really moved, rather than being re-sent at the same level.
    assert [str(bars[-1].stop_price) for bars in his[6:]] == ["106.0", "107.0", "106.0"]


def test_the_line_bending_back_withdraws_the_order() -> None:
    signals = _drive(
        Mme9TurnStrategy(side=Side.LONG, period=3),
        _candles(["100", "99", "98", "97", "96", "95", "105", "104", "90"]),
    )

    assert _kinds(signals)[6:] == [["ENTRY"], [], ["CANCEL"]]
    (cancel,) = signals[8]
    assert cancel.client_id == signals[6][0].client_id
    assert cancel.reason == "cancel.mme9turn"


def test_a_flat_average_does_not_cancel_the_order() -> None:
    """A bar closing exactly on the average leaves the line where it was.

    An EMA moves by `a * (close - previous)`, so a close *equal* to the previous average leaves it
    unchanged to the last digit. Reading that as "no longer rising" — the obvious way to write the
    check — would withdraw a live order on the one bar where nothing happened.
    """
    signals = _drive(
        Mme9TurnStrategy(side=Side.LONG, period=3),
        # The average is 100.5 after the turn, and bar 7 closes there exactly.
        _candles(["100", "99", "98", "97", "96", "95", "105", "100.5", "101"]),
    )

    assert _kinds(signals)[6:] == [["ENTRY"], [], []]


def test_a_flat_average_does_not_arm_while_the_line_is_falling() -> None:
    """The other half of "flat does not turn", and the half that costs money.

    ⚠️ The flat bar with an order already resting only proves that flat does not *cancel*: the
    line was rising there, so reading flat as "rising" and reading it as "unchanged" answer the
    same. The separating case is a flat bar inside a **fall** — the line merely stopped — where
    treating it as a rise buys the high of a bar in a downtrend and nothing in the numbers looks
    wrong afterwards.

    The last bar is what keeps the rest from being silence out of a stopped machine, and it pins
    the *other* rival reading besides: flat as "direction unknown". Under that one the fall is
    forgotten, the real turn on bar 5 has nothing to have turned from, and the setup quietly stops
    appearing after any pause of the average.
    """
    signals = _drive(
        Mme9TurnStrategy(side=Side.LONG, period=3),
        # Bar 4 closes exactly on the average (98.0), which leaves it at 98.00 to the last digit.
        # Bar 5 then bends the line up for real.
        _candles(["100", "99", "98", "97", "98.0", "105"]),
    )

    assert _kinds(signals) == [[], [], [], [], [], ["ENTRY"]]
    assert str(signals[5][0].stop_price) == "106.0"


def test_a_flat_average_does_not_withdraw_a_resting_sell() -> None:
    """The mirror of the same guard, from the side where flat would read as *against* the setup."""
    signals = _drive(
        Mme9TurnStrategy(side=Side.SHORT, period=3),
        # The average is 99.5 after the turn down, and bar 7 closes there exactly.
        _candles(["100", "101", "102", "103", "104", "105", "95", "99.5", "98"]),
    )

    assert _kinds(signals)[6:] == [["ENTRY"], [], []]


def test_the_first_reading_of_the_average_cannot_be_a_turn() -> None:
    """The average's first value has nothing behind it to have turned from.

    The same entry toll every recurrence pays: two readings make a slope, three make a change of
    slope, and until then the setup records and does not trade. A class that armed on its first
    comparable bar would open a trade out of the warmup.
    """
    # Rising from the first value the MME3 can produce, with no fall before it.
    signals = _drive(
        Mme9TurnStrategy(side=Side.LONG, period=3),
        _candles(["100", "101", "102", "103", "104", "105"]),
    )

    assert _kinds(signals) == [[], [], [], [], [], []]


def test_a_bar_closing_back_across_the_average_does_not_move_the_stop() -> None:
    """The published setup states an entry and a stop and stops talking, and so does this class.

    ⚠️ The separator against the author's again: on this exact bar his tightens the stop onto the
    bar's low. Here the open trade is left alone.
    """
    held = Position(
        symbol=AAPL.symbol,
        side=Side.LONG,
        volume=Decimal(1),
        entry_price=Decimal("106.0"),
        entry_time=_candles(["100"])[0].time,
        stop_loss=Decimal("103.50"),
        initial_stop_loss=Decimal("103.50"),
    )
    candles = _candles(_CLOSES_BACK)

    published = _drive(
        Mme9TurnStrategy(side=Side.LONG, period=3),
        candles,
        position_on=frozenset({7, 8}),
        held=held,
    )
    assert _kinds(published)[7:] == [[], []]

    his = _drive(
        Mme9BreakoutStrategy(side=Side.LONG, period=3, breakeven_at_r=None),
        candles,
        position_on=frozenset({7, 8}),
        held=held,
    )
    (moved,) = his[8]
    assert moved.kind is SignalKind.MODIFY_STOP
    assert str(moved.stop_loss) == "104.00"


def test_breakeven_is_off_by_default_and_arms_when_it_is_asked_for() -> None:
    """`breakeven_at_r` is the one rule here that is a number, and it starts switched off."""
    held = Position(
        symbol=AAPL.symbol,
        side=Side.LONG,
        volume=Decimal(1),
        entry_price=Decimal("106.0"),
        entry_time=_candles(["100"])[0].time,
        stop_loss=Decimal("103.50"),
        initial_stop_loss=Decimal("103.50"),
    )
    candles = _candles(_CLOSES_BACK)

    off = _drive(
        Mme9TurnStrategy(side=Side.LONG, period=3),
        candles,
        position_on=frozenset({7}),
        held=held,
    )
    assert off[7] == []

    on = _drive(
        Mme9TurnStrategy(side=Side.LONG, period=3, breakeven_at_r=Decimal(2)),
        candles,
        position_on=frozenset({7}),
        held=held,
    )
    (moved,) = on[7]
    assert moved.kind is SignalKind.MODIFY_STOP
    # Risk was 2.50, so twice it is 111.00, and bar 7 reaches 113 — the stop goes to the entry.
    assert str(moved.stop_loss) == "106.0"


def test_a_turn_that_lands_while_a_trade_is_open_is_lost() -> None:
    """Documented behaviour, not an oversight: arming is one bar's event and that bar is taken.

    By the time the account is flat the bar that bent the line is history and its high may be far
    above price. Carrying the reference forward would be inventing a rule the literature does not
    state.
    """
    held = Position(
        symbol=AAPL.symbol,
        side=Side.LONG,
        volume=Decimal(1),
        entry_price=Decimal("106.0"),
        entry_time=_candles(["100"])[0].time,
    )
    # Bar 10 bends the line back up, and it is the bar the position is open on. Bar 11 keeps the
    # line rising without bending it, so nothing arms there either.
    signals = _drive(
        Mme9TurnStrategy(side=Side.LONG, period=3),
        _candles(["100", "99", "98", "97", "96", "95", "105", "104", "90", "91", "108", "109"]),
        position_on=frozenset({10}),
        held=held,
    )

    assert _kinds(signals)[9:] == [[], [], []]


def test_the_stop_order_fills_on_a_later_bar_at_the_trigger() -> None:
    """Through the real loop, which is the only place a breakout is a breakout.

    The order is placed on the bar that bent the line and cannot execute on it: the decision is
    taken at that bar's close, and the fill belongs to a bar that has not happened yet.
    """
    result = _run(_candles(_TURN_UP))

    entries = [fill for fill in result.fills if fill.order.intent is SignalKind.ENTRY]
    assert [str(fill.price) for fill in entries] == ["106.0"]
    (filled,) = entries
    assert filled.time > _candles(_TURN_UP)[6].time


@given(
    st.lists(
        st.decimals(min_value="50", max_value="150", places=2),
        min_size=30,
        max_size=120,
    )
)
@settings(max_examples=50, deadline=None)
def test_slope_and_close_are_the_same_event_on_an_ema(closes: list[Decimal]) -> None:
    """Why the arming tests above cannot be the separator between the two 9.1s.

    `ema = previous + a(close - previous)`, so `ema > previous` and `close > ema` are the same
    inequality wearing different clothes — the second is the first multiplied by `(1 - a)`, which
    is positive. Held over generated streams rather than one series, because a single fixture
    agreeing would prove nothing about the claim.

    It is also the reason the differences between these setups had to be looked for *after* the
    order is placed. On an **arithmetic** average the two readings genuinely diverge, since an SMA
    turns on the bar leaving its window and the newest close alone cannot decide that.
    """
    average = EMA(period=9, source="close")
    previous = None
    compared = 0

    with localcontext(ENGINE_CONTEXT):
        for candle in _candles([str(close) for close in closes]):
            average.update(candle)
            value = average.value()
            if value is None:
                continue
            if previous is not None:
                compared += 1
                assert (value > previous) == (candle.close > value)
            previous = value

    assert compared >= len(closes) - 9


def test_the_two_readings_come_apart_at_a_period_of_one() -> None:
    """The one place the identity above fails, and it is reachable from the DSL.

    `alpha = 2 / (period + 1)`, so at `period = 1` the alpha is 1 and the average is the close
    itself. `close > ema` is then never true, which leaves the author's setup unable to arm at
    all, while this one — reading the slope — arms on the close that rises after a fall. Pinned
    rather than forbidden: the schema allows `period >= 1`, and a corner that silently means
    something different is worth an address.
    """
    candles = _candles(["100", "99", "98", "105", "106"])

    published = _drive(Mme9TurnStrategy(side=Side.LONG, period=1), candles)
    assert _kinds(published) == [[], [], [], ["ENTRY"], []]

    his = _drive(Mme9BreakoutStrategy(side=Side.LONG, period=1), candles)
    assert _kinds(his) == [[], [], [], [], []]


def test_a_fill_frees_the_setup_without_cancelling_the_order_it_consumed() -> None:
    """The armed name is dropped the moment a fill shows, so nothing cancels a live trade's order.

    Two channels say a fill happened (ADR-0015) and this drives the second, a position the
    strategy was never shown: bar 8 bends the line back down, and the only reason it emits nothing
    is that the name was already forgotten.
    """
    held = Position(
        symbol=AAPL.symbol,
        side=Side.LONG,
        volume=Decimal(1),
        entry_price=Decimal("106.0"),
        entry_time=_candles(["100"])[0].time,
    )
    signals = _drive(
        Mme9TurnStrategy(side=Side.LONG, period=3),
        _candles(["100", "99", "98", "97", "96", "95", "105", "104", "90"]),
        position_on=frozenset({7}),
        held=held,
    )

    assert _kinds(signals)[6:] == [["ENTRY"], [], []]


def test_the_fill_channel_is_read_as_well_as_the_position() -> None:
    """The other channel: a fill carrying the armed name, on a bar that shows no position.

    It is the only sign that survives a trade opening and closing inside one bar, and reading only
    `context.position` would leave the strategy holding a name whose order the trade already
    consumed — which the next bend of the line would cancel, at a broker that no longer holds it.
    """
    strategy = Mme9TurnStrategy(side=Side.LONG, period=3)
    candles = _candles(["100", "99", "98", "97", "96", "95", "105", "104", "90"])
    armed: Signal | None = None
    emitted: list[list[Signal]] = []

    with localcontext(ENGINE_CONTEXT):
        for index, candle in enumerate(candles):
            fills: tuple[Fill, ...] = ()
            if index == 7 and armed is not None:
                fills = (
                    Fill(
                        order=OrderRequest(
                            symbol=AAPL.symbol,
                            side=Side.LONG,
                            intent=SignalKind.ENTRY,
                            volume=Decimal(1),
                            decided_at=candles[6].time,
                            client_id=armed.client_id,
                        ),
                        time=candle.time,
                        price=Decimal("106.0"),
                        volume=Decimal(1),
                        costs=Decimal(0),
                    ),
                )
            context = Context(
                candle=candle, instrument=AAPL, account=_ACCOUNT, position=None, fills=fills
            )
            signals = list(strategy.on_bar(context))
            emitted.append(signals)
            if signals and signals[0].kind is SignalKind.ENTRY:
                armed = signals[0]

    # Bar 8 bends the line back down. With the fill seen, there is no name left to cancel.
    assert _kinds(emitted)[6:] == [["ENTRY"], [], []]

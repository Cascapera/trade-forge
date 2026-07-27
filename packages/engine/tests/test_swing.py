"""The MME9 breakout setup: enter on the break of the candle that turned the average.

These drive the strategy through the real `run()` loop rather than poking it bar by bar, because
the thing that makes a breakout a breakout — the stop order filling on a *later* bar, at the
trigger, under the anti-lookahead guard — only exists once the loop and broker are in the picture.
The candles are hand-built so the MME3 (a short average, to keep the warmup readable) crosses
where the comment says it does; the golden numbers come from feeding these exact bars to the
engine, never from arithmetic done here.
"""

import datetime as dt
from decimal import Decimal, localcontext
from itertools import pairwise

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from tradeforge_engine.backtest_broker import BacktestBroker
from tradeforge_engine.domain import (
    ZERO,
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
from tradeforge_engine.swing import Mme9BreakoutStrategy
from tradeforge_engine.testing import AAPL, HOUR, START, ImmediateFillBroker, bar

_ACCOUNT = ImmediateFillBroker(instrument=AAPL).account()


@st.composite
def _random_candles(draw: st.DrawFn) -> list[Candle]:
    """A stream of valid candles: each contains its own body, and every price is positive. Enough
    of them to warm up the MME3 and leave room for several turns."""
    count = draw(st.integers(min_value=4, max_value=25))
    price = st.decimals(min_value="50", max_value="150", places=1)
    wick = st.decimals(min_value="0", max_value="4", places=1)
    candles: list[Candle] = []
    for index in range(count):
        open_ = draw(price)
        close = draw(price)
        high = max(open_, close) + draw(wick)
        low = min(open_, close) - draw(wick)
        candles.append(bar(index, open_=str(open_), close=str(close), high=str(high), low=str(low)))
    return candles


def _run(  # noqa: PLR0913 — keyword-only; each names one axis of a backtest
    candles: list[Candle],
    *,
    side: Side = Side.LONG,
    rr: str | None = "2",
    percent: str = "1",
    period: int = 3,
    stop_buffer_ticks: int = 0,
) -> RunResult:
    strategy = Mme9BreakoutStrategy(side=side, period=period, stop_buffer_ticks=stop_buffer_ticks)
    broker = BacktestBroker(
        instrument=AAPL,
        initial_capital=Decimal(100_000),
        take_profit_rr=Decimal(rr) if rr is not None else None,
    )
    risk = PercentRiskManager(percent=Decimal(percent))
    return run(
        candles=candles,
        timeframe=HOUR,
        instrument=AAPL,
        strategy=strategy,
        broker=broker,
        risk=risk,
    )


def _entries(result: RunResult) -> list[Fill]:
    return [f for f in result.fills if f.order.intent is SignalKind.ENTRY]


def _drive(
    strategy: Mme9BreakoutStrategy,
    candles: list[Candle],
    *,
    position_on: frozenset[int] = frozenset(),
    fills_on: dict[int, list[Fill]] | None = None,
) -> list[list[Signal]]:
    """Feed candles one at a time and collect the signals each bar produced.

    Most of this suite drives the real `run()` loop, which is the honest way to test a breakout.
    But a *fill* is what spends a turn, and the strategy learns of one through two channels the
    loop can only exercise together: `context.fills` and, as a fallback, `context.position`
    (ADR-0015). Driving by hand is the only way to hold one channel still and move the other —
    and the only way to see the bars that emit *nothing*, which is where the silent bugs live.
    """
    fills_on = fills_on or {}
    out: list[list[Signal]] = []
    with localcontext(ENGINE_CONTEXT):
        for index, candle in enumerate(candles):
            position = None
            if index in position_on:
                position = Position(
                    symbol=AAPL.symbol,
                    side=Side.LONG,
                    volume=Decimal(1),
                    entry_price=candle.open,
                    entry_time=candle.time,
                )
            context = Context(
                candle=candle,
                instrument=AAPL,
                account=_ACCOUNT,
                position=position,
                fills=tuple(fills_on.get(index, ())),
            )
            out.append(list(strategy.on_bar(context)))
    return out


def _s10(index: int, *, open_: str, close: str, high: str, low: str) -> Candle:
    """One candle, `index` *ten-second* steps after the start — the only helper here that is not
    hourly, because two bars sharing a minute is the case the order names have to survive."""
    return Candle(
        time=START + index * dt.timedelta(seconds=10),
        open=Decimal(open_),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
    )


def _fill_named(candle: Candle, client_id: str) -> Fill:
    """A fill carrying someone's name — the strategy's own, or a stranger's."""
    order = OrderRequest(
        symbol=AAPL.symbol,
        side=Side.LONG,
        intent=SignalKind.ENTRY,
        volume=Decimal(1),
        decided_at=candle.time,
        client_id=client_id,
    )
    return Fill(order=order, time=candle.time, price=candle.close, volume=Decimal(1), costs=ZERO)


# The seed every long scenario shares: three bars whose closes (100, 99, 98) leave the MME3 at 99.
# Bar 2 is the last warmup bar; nothing can arm before it, because there is no average to close
# across yet.
_SEED = [
    bar(0, open_="100", close="100", high="100.5", low="99.5"),
    bar(1, open_="100", close="99", high="100.2", low="98.8"),
    bar(2, open_="99", close="98", high="99.2", low="97.5"),  # MME3 seeds at 99 here
]


# --------------------------------------------------------------------------- #
# Warmup                                                                        #
# --------------------------------------------------------------------------- #


def test_nothing_arms_while_the_average_is_warming_up() -> None:
    """The MME3 has no value until its third bar, and a bar cannot have closed across an average
    that does not exist yet. A close above bar 1 that would obviously 'cross' produces nothing."""
    result = _run([*_SEED[:2], bar(2, open_="99", close="105", high="105.2", low="98.9")])
    assert result.fills == ()


# --------------------------------------------------------------------------- #
# The entry, the golden                                                         #
# --------------------------------------------------------------------------- #


def test_a_long_enters_on_the_break_of_the_reference_high() -> None:
    """The setup, end to end. Bar 3 closes 101, above the MME3 of 100 — the average turned up, and
    its own high (101.3) is the trigger, its low (97.9) the stop. Bar 4 breaks 101.3, so the order
    fills there; bar 7 closes back below and the trade is stopped at 97.9. Entry at the reference
    high, stop at the reference low — the whole reference bar is the trade."""
    candles = [
        *_SEED,
        bar(3, open_="98", close="101", high="101.3", low="97.9"),  # cross up; trigger 101.3
        bar(4, open_="101", close="103", high="103.5", low="100.8"),  # breaks 101.3 -> fill
        bar(5, open_="103", close="105", high="105.2", low="102.9"),
        bar(6, open_="105", close="102", high="105.4", low="101.9"),
        bar(7, open_="102", close="97", high="102.2", low="96.9"),  # low tags the stop 97.9
    ]
    result = _run(candles)
    [entry] = _entries(result)
    assert entry.order.side is Side.LONG
    assert entry.price == Decimal("101.3")
    assert entry.order.stop_loss == Decimal("97.9")
    # the exit is the broker's protective stop at the reference low
    [exit_fill] = [f for f in result.fills if f.order.intent is SignalKind.EXIT]
    assert exit_fill.order.reason == "sl"
    assert exit_fill.price == Decimal("97.9")


def test_the_entry_cannot_fill_on_the_bar_that_armed_it() -> None:
    """Anti-lookahead, where a stop order can actually break it: the order carries the decision
    instant of the bar that placed it, and the fill must land on a *later* bar. Bar 3 arms and its
    own range already reaches the trigger — but the fill is dated bar 4, never bar 3."""
    candles = [
        *_SEED,
        bar(3, open_="98", close="101", high="101.3", low="97.9"),
        bar(4, open_="101", close="103", high="103.5", low="100.8"),
    ]
    result = _run(candles)
    [entry] = _entries(result)
    assert entry.time == candles[4].time
    assert entry.time > candles[3].time


# --------------------------------------------------------------------------- #
# Rearm: the trigger follows the latest bar                                     #
# --------------------------------------------------------------------------- #


def test_an_unfilled_order_rearms_to_the_latest_bar_on_the_setup_side() -> None:
    """The author's 'vale sempre a última barra'. Bar 3 arms at its high 101.3; bar 4 closes above
    the average too but its high (101.2) never reaches 101.3, so the order did not fill — and the
    reference moves to bar 4, trigger 101.2 and stop at bar 4's low 100.9. Bar 5 breaks 101.2, so
    the fill is 101.2, not bar 3's 101.3, and the target is measured off bar 4's tighter range."""
    candles = [
        *_SEED,
        bar(3, open_="98", close="101", high="101.3", low="97.9"),  # arm trigger 101.3
        bar(
            4, open_="101", close="101", high="101.2", low="100.9"
        ),  # rearm trigger 101.2, stop 100.9
        bar(5, open_="101", close="104", high="104.5", low="100.95"),  # breaks 101.2 -> fill
    ]
    result = _run(candles)
    [entry] = _entries(result)
    assert entry.price == Decimal("101.2")
    assert entry.order.stop_loss == Decimal("100.9")
    # target is 2R off the reference's own range (101.2 - 100.9 = 0.3) -> 101.8
    [exit_fill] = [f for f in result.fills if f.order.intent is SignalKind.EXIT]
    assert exit_fill.order.reason == "tp"
    assert exit_fill.price == Decimal("101.8")


# --------------------------------------------------------------------------- #
# Cancel: the setup dies when price closes back across                          #
# --------------------------------------------------------------------------- #


def test_a_close_back_across_the_average_withdraws_the_order() -> None:
    """The order's life is the turn's life. Bar 3 arms at 101.3; bar 4 closes 97, back below the
    MME3, so the setup is gone and the order is withdrawn *that* bar — the cancel is applied before
    the next bar can fill. Bar 5 breaks 101.3, but there is nothing resting to take it."""
    candles = [
        *_SEED,
        bar(3, open_="98", close="101", high="101.3", low="97.9"),  # arm
        bar(4, open_="100", close="97", high="100.4", low="96.9"),  # closes back below -> withdraw
        bar(
            5, open_="97", close="99", high="101.9", low="96.9"
        ),  # would break 101.3, but cancelled
    ]
    result = _run(candles)
    assert result.fills == ()


# --------------------------------------------------------------------------- #
# One trade per turn                                                            #
# --------------------------------------------------------------------------- #


def test_a_turn_gives_one_trade_and_does_not_rearm_until_a_fresh_cross() -> None:
    """Once the order fills, the turn is spent: no second entry while price merely stays above the
    average. Bar 3 arms, bar 4 fills, bar 5 spikes to the target and closes the trade — still above
    the MME3. Bars 6 and 7 stay above and must arm nothing. Only bar 8 (a close back below) and
    bar 9 (a cross back up) open a new turn, and its entry is the second one in the run."""
    candles = [
        *_SEED,
        bar(
            3, open_="98", close="101", high="101.3", low="97.9"
        ),  # cross; trigger 101.3, stop 97.9
        bar(4, open_="101", close="102", high="102.0", low="100.8"),  # fill at 101.3
        bar(
            5, open_="102", close="107", high="108.5", low="101.9"
        ),  # target 108.1 hit; still > MME3
        bar(
            6, open_="107", close="106", high="107.5", low="105.5"
        ),  # above -> must NOT rearm (spent)
        bar(
            7, open_="106", close="105", high="106.2", low="104.5"
        ),  # above -> must NOT rearm (spent)
        bar(
            8, open_="105", close="90", high="105.1", low="89.5"
        ),  # closes far below -> turn resets
        bar(9, open_="90", close="99", high="99.5", low="89.9"),  # cross back up -> new turn arms
        bar(10, open_="99", close="102", high="103.0", low="98.9"),  # breaks bar9 high 99.5 -> fill
    ]
    result = _run(candles)
    entries = _entries(result)
    assert len(entries) == 2
    assert entries[0].price == Decimal("101.3")  # first turn
    # second turn: bar 9 is the reference (its high 99.5 is the trigger), filled on bar 10
    assert entries[1].price == Decimal("99.5")
    assert entries[1].time == candles[10].time


# --------------------------------------------------------------------------- #
# The short mirror                                                              #
# --------------------------------------------------------------------------- #


def test_a_short_enters_on_the_break_of_the_reference_low() -> None:
    """The mirror: a bar closes *below* the average, and the order rests at its low waiting for
    price to break down through it. Bar 3 closes 99 under the MME3 of 100.5; its low 98.7 is the
    trigger, its high 102.1 the stop. Bar 4 breaks 98.7 and the short fills there."""
    candles = [
        bar(0, open_="100", close="100", high="100.5", low="99.5"),
        bar(1, open_="100", close="101", high="101.2", low="99.8"),
        bar(2, open_="101", close="102", high="102.5", low="100.8"),  # MME3 seeds at 101
        bar(
            3, open_="102", close="99", high="102.1", low="98.7"
        ),  # cross down; trigger 98.7, stop 102.1
        bar(4, open_="99", close="96", high="99.2", low="95.5"),  # breaks 98.7 -> fill
    ]
    result = _run(candles, side=Side.SHORT)
    [entry] = _entries(result)
    assert entry.order.side is Side.SHORT
    assert entry.price == Decimal("98.7")
    assert entry.order.stop_loss == Decimal("102.1")


# --------------------------------------------------------------------------- #
# The exit is the broker's — the setup only enters                             #
# --------------------------------------------------------------------------- #


def test_with_no_take_profit_only_the_stop_can_close_the_trade() -> None:
    """The strategy never emits an exit; the exit modes are the broker's. With no `take_profit_rr`
    the position carries only a stop, so a bar that would have tagged a 2R target just holds."""
    candles = [
        *_SEED,
        bar(3, open_="98", close="101", high="101.3", low="97.9"),
        bar(4, open_="101", close="103", high="103.5", low="100.8"),  # fill 101.3
        bar(
            5, open_="103", close="108", high="108.5", low="102.9"
        ),  # would tag a 2R target (108.1)
    ]
    result = _run(candles, rr=None)
    assert len(_entries(result)) == 1
    # no exit fill: with no target armed, nothing closed the trade this run
    assert [f for f in result.fills if f.order.intent is SignalKind.EXIT] == []


# --------------------------------------------------------------------------- #
# Sizing and geometry                                                          #
# --------------------------------------------------------------------------- #


def test_the_position_is_sized_against_the_reference_range_not_the_close() -> None:
    """ADR-0016 sizes from the trigger, and here the trigger and the stop are the reference bar's
    high and low — so the risk is that bar's range. At 1% of 100k the loss on a stop-out is ~1000,
    whatever the close that decided the order was."""
    candles = [
        *_SEED,
        bar(3, open_="98", close="101", high="101.3", low="97.9"),  # range 3.4
        bar(4, open_="101", close="103", high="103.5", low="100.8"),  # fill 101.3
        bar(5, open_="103", close="97", high="103.2", low="96.9"),  # stop out at 97.9
    ]
    result = _run(candles)
    [trade] = result.trades
    assert trade.reason == "sl"
    # entry 101.3, stop 97.9: a full-R loss on 1% of 100k, within a cent of -1000
    assert trade.net_pnl < ZERO
    assert abs(trade.net_pnl + Decimal(1000)) < Decimal(1)


def test_a_reference_bar_with_no_range_arms_nothing() -> None:
    """A bar whose high equals its low is a single price: the trigger would sit on the stop and
    the trade would carry no risk — a division by zero in sizing, not a free trade. It is not a
    reference, and the setup waits for a bar that has a range.

    Asserted on the **signals the bar emitted**, not on the fills the run produced. Asking "did
    anything fill off bar 3" is a question the sizing layer answers too — a zero-distance order is
    vetoed downstream by `PercentRiskManager` — so a fill-based assertion passes with this guard
    deleted. The strategy's own silence is the only thing that pins the strategy's own guard.
    """
    candles = [
        *_SEED,
        bar(3, open_="101", close="101", high="101", low="101"),  # closes above MME3 but no range
        bar(4, open_="101", close="103", high="103.5", low="100.8"),
    ]
    signals = _drive(Mme9BreakoutStrategy(period=3), candles)

    assert signals[3] == []  # on-side, but no range: not a reference
    # bar 4 has a range and closes above, so it is the first real reference
    [armed] = signals[4]
    assert armed.kind is SignalKind.ENTRY
    assert armed.stop_price == Decimal("103.5")
    assert armed.stop_loss == Decimal("100.8")


def test_a_bar_with_no_range_keeps_the_order_already_resting() -> None:
    """The other half of the no-range rule: it is a *skip*, not a withdrawal. Bar 3 arms at 101.3;
    bar 4 is a single price (a thin session, a halt) and closes above the average — it cannot be
    the new reference, and taking 'vale sempre a última barra' literally here would cancel a live
    order and replace it with nothing. Bar 5 breaks 101.3 and the trade is bar 3's, unchanged."""
    candles = [
        *_SEED,
        bar(3, open_="98", close="101", high="101.3", low="97.9"),  # arm trigger 101.3
        bar(4, open_="101", close="101", high="101", low="101"),  # no range: skipped, not cancelled
        bar(5, open_="101", close="104", high="104.5", low="100.9"),  # breaks 101.3 -> fill
    ]
    result = _run(candles)
    [entry] = _entries(result)
    assert entry.price == Decimal("101.3")
    assert entry.order.stop_loss == Decimal("97.9")
    assert entry.time == candles[5].time


def test_a_no_range_bar_keeps_the_name_the_order_will_be_withdrawn_by() -> None:
    """Skipping a no-range bar has to keep the *name*, not just the order.

    Bar 3 arms at 101.3 and bar 4 is a single price, so nothing changes. Bar 5 closes back across
    the average and the setup dies — and withdrawing it needs the name bar 3 gave it. Forgetting
    the name on the skipped bar leaves the order alive in the broker with nobody able to cancel
    it: a ghost that fills on bar 6's break of a level the strategy stopped wanting two bars ago.
    That is why the assertion is `no fills at all`, and why the cancel is checked by name.
    """
    candles = [
        *_SEED,
        bar(3, open_="98", close="101", high="101.3", low="97.9"),  # arm trigger 101.3
        bar(4, open_="101", close="101", high="101", low="101"),  # no range: skipped
        bar(5, open_="101", close="97", high="101.0", low="96.5"),  # closes below -> withdraw
        bar(6, open_="97", close="99", high="102.0", low="96.9"),  # would break 101.3 if it lived
    ]
    signals = _drive(Mme9BreakoutStrategy(period=3), candles)
    [armed] = signals[3]
    assert signals[4] == []
    [withdraw] = signals[5]
    assert withdraw.kind is SignalKind.CANCEL
    assert withdraw.client_id == armed.client_id
    # One signal on bar 6, not two: a name already withdrawn is not withdrawn again. Holding a
    # dead name costs nothing in a backtest, but in live it is a wasted round trip and — worse —
    # it is the state in which `_observe_fill`'s position fallback would spend a turn on an order
    # that was cancelled and never traded.
    [rearm] = signals[6]
    assert rearm.kind is SignalKind.ENTRY

    assert _run(candles).fills == ()


def test_two_references_inside_one_minute_get_different_names() -> None:
    """The name is a counter, not a timestamp — and below M1 that is the whole difference.

    On S10 bars, two references land in the same minute, so a name built from the timestamp alone
    repeats. The broker keeps consumed names to refuse duplicates, so the second order would be
    rejected while the strategy believed itself armed: no entry, no error, and nothing in the
    report to say a trade was dropped. The counter never resets, so the names cannot collide.
    """
    candles = [
        _s10(0, open_="100", close="100", high="100.5", low="99.5"),
        _s10(1, open_="100", close="99", high="100.2", low="98.8"),
        _s10(2, open_="99", close="98", high="99.2", low="97.5"),  # MME3 seeds at 99
        _s10(3, open_="98", close="101", high="101.3", low="97.9"),  # arm, at 00:00:30
        _s10(4, open_="101", close="101", high="101.2", low="100.9"),  # rearm, at 00:00:40
    ]
    signals = _drive(Mme9BreakoutStrategy(period=3), candles)

    [armed] = signals[3]
    withdraw, rearm = signals[4]
    assert candles[3].time.replace(second=0) == candles[4].time.replace(second=0)  # same minute
    assert withdraw.client_id == armed.client_id
    assert rearm.client_id != armed.client_id


def test_the_stop_buffer_pushes_the_stop_past_the_reference_edge() -> None:
    """The optional buffer, in ticks (AAPL's tick is 0.01), moves the stop off the bar's own edge —
    a stop *on* the low is taken by the noise of the bar that set it. Two ticks below 97.9 is
    97.88, and the risk widens by the same two ticks."""
    candles = [
        *_SEED,
        bar(3, open_="98", close="101", high="101.3", low="97.9"),
        bar(4, open_="101", close="103", high="103.5", low="100.8"),
    ]
    result = _run(candles, stop_buffer_ticks=2)
    [entry] = _entries(result)
    assert entry.order.stop_loss == Decimal("97.88")


def test_the_stop_buffer_mirrors_above_the_reference_high_on_a_short() -> None:
    """The short half of the buffer, which symmetry is not allowed to vouch for: a sell is
    protected *above* the reference bar, so the buffer has to be **added**. Two ticks above bar 3's
    high of 102.1 is 102.12. Subtracting instead of adding — or dropping the buffer on this branch
    — puts the stop exactly on the high of the very bar that created the setup, which is the level
    that bar's own noise already reached: the one failure the buffer exists to prevent, alive only
    in shorts and invisible to every long test in this file."""
    candles = [
        bar(0, open_="100", close="100", high="100.5", low="99.5"),
        bar(1, open_="100", close="101", high="101.2", low="99.8"),
        bar(2, open_="101", close="102", high="102.5", low="100.8"),  # MME3 seeds at 101
        bar(3, open_="102", close="99", high="102.1", low="98.7"),  # cross down; trigger 98.7
        bar(4, open_="99", close="96", high="99.2", low="95.5"),  # breaks 98.7 -> fill
    ]
    result = _run(candles, side=Side.SHORT, stop_buffer_ticks=2)
    [entry] = _entries(result)
    assert entry.price == Decimal("98.7")
    assert entry.order.stop_loss == Decimal("102.12")


# --------------------------------------------------------------------------- #
# Construction guards                                                          #
# --------------------------------------------------------------------------- #


def test_a_stop_that_the_buffer_drives_to_zero_or_below_arms_nothing() -> None:
    """A non-positive stop is not a wide stop — it is *no* stop, because `low <= stop` can never be
    true and the trade would run unprotected. On penny-priced bars a two-cent low with a three-tick
    buffer would put the stop at -0.01, so the reference is refused; the same bars with no buffer
    arm normally, at 0.11 over a stop at 0.02."""
    candles = [
        bar(0, open_="0.10", close="0.10", high="0.11", low="0.09"),
        bar(1, open_="0.10", close="0.09", high="0.10", low="0.08"),
        bar(2, open_="0.09", close="0.08", high="0.09", low="0.07"),  # MME3 seeds 0.09
        bar(3, open_="0.08", close="0.11", high="0.11", low="0.02"),  # on-side; low 0.02
        bar(4, open_="0.11", close="0.10", high="0.11", low="0.09"),  # breaks 0.11 if armed
    ]
    armed = _entries(_run(candles, stop_buffer_ticks=0))
    assert [(e.price, e.order.stop_loss) for e in armed] == [(Decimal("0.11"), Decimal("0.02"))]
    # three ticks below 0.02 is negative: the guard refuses bar 3, and nothing fills off it
    guarded = _run(candles, stop_buffer_ticks=3)
    assert guarded.fills == ()
    # And zero itself is refused, not allowed through: `low <= 0` can never be true, so a stop at
    # 0.00 is not a very wide stop, it is no stop. One tick above it still arms — which is what
    # makes this the boundary and not just "negative is bad".
    assert _entries(_run(candles, stop_buffer_ticks=1))[0].order.stop_loss == Decimal("0.01")
    assert _run(candles, stop_buffer_ticks=2).fills == ()


def test_a_stop_out_that_closes_on_the_setup_side_does_not_rearm() -> None:
    """The delicate half of one-trade-per-turn, isolated: a turn is spent by its *fill*, and only
    a close back across the average reopens it. Bar 3 arms and bar 4 fills at 101.3; bar 5 tags the
    stop at the reference low 97.5 but **closes at 101.5, still above the MME3**. Bars 6 and 7 stay
    above too. Nothing rearms — without `_spent` the strategy would re-buy the same level it was
    just stopped out of, which is the martingale this rule exists to refuse."""
    candles = [
        *_SEED,
        bar(
            3, open_="98", close="101", high="101.3", low="97.5"
        ),  # cross; trigger 101.3, stop 97.5
        bar(4, open_="101", close="102", high="102.2", low="100.8"),  # fill 101.3
        bar(
            5, open_="102", close="101.5", high="102.3", low="97.5"
        ),  # stop-out, but closes above MME3
        bar(6, open_="101.5", close="102", high="102.4", low="101.0"),  # above -> must NOT rearm
        bar(7, open_="102", close="102.5", high="103.0", low="101.5"),  # above -> must NOT rearm
    ]
    result = _run(candles)
    entries = _entries(result)
    assert len(entries) == 1
    assert entries[0].price == Decimal("101.3")


def test_a_trade_that_opened_and_died_inside_one_bar_still_spends_the_turn() -> None:
    """The invisible fill, and the only thing standing between this setup and a martingale.

    Bar 4 breaks 101.3 and then tags the stop at 97.9 — the position is born and buried inside
    that one bar, so by the time the strategy runs, `context.position` is already `None`. The
    only trace left is the bar's own fills (ADR-0015). Bar 4 also closes at 101.2, **still above
    the MME3 of 100.60**, so nothing resets the turn: the setup must stay quiet.

    Without the `context.fills` half of `_observe_fill`, the turn looks untouched, bar 4 becomes
    the new reference (trigger 101.5) and bar 5 re-buys — one bar after being stopped out, inside
    the same turn, which is exactly the re-entry rule 3 exists to refuse. The mirror of
    `test_setups.py::test_a_trade_that_opened_and_died_inside_one_bar_still_spends_its_zone`.
    """
    candles = [
        *_SEED,
        bar(3, open_="98", close="101", high="101.3", low="97.9"),  # arm: trigger 101.3, stop 97.9
        bar(
            4, open_="101", close="101.2", high="101.5", low="97.8"
        ),  # fills 101.3 AND stops 97.9; closes 101.2, above the MME3
        bar(5, open_="101.2", close="102", high="102.5", low="101.0"),  # would break 101.5
    ]
    result = _run(candles)

    entries = _entries(result)
    assert len(entries) == 1
    assert entries[0].price == Decimal("101.3")
    # the whole trade lived on bar 4: entry and exit share the bar, which is what makes it invisible
    [exit_fill] = [f for f in result.fills if f.order.intent is SignalKind.EXIT]
    assert exit_fill.order.reason == "sl"
    assert exit_fill.time == entries[0].time == candles[4].time


def test_a_turn_that_ends_while_the_trade_is_open_is_a_real_turn() -> None:
    """The turn boundary is read on every bar — the average does not know we are positioned.

    Bar 4 fills; bar 5 closes 99.5, **below** the MME3 of 100.25, while the trade is still open:
    that turn is over. Bar 6 closes back above, which starts a new one. Bar 7 hits the 2R target
    (108.10) — the broker closes the trade in loop step 1, so the strategy already sees a flat
    account on bar 7 — and bar 7 is on-side with a range, so it arms the new turn at its own high.

    Read only while flat, that boundary is invisible: bar 5 is swallowed by the open-position
    early return, the turn stays spent, and bars 7-9 arm nothing at all. Nothing comes out wrong —
    the trade simply never appears, and how many disappear depends on where each exit happens to
    fall relative to the average.
    """
    candles = [
        *_SEED,
        bar(3, open_="98", close="101", high="101.3", low="97.9"),  # arm: trigger 101.3, stop 97.9
        bar(4, open_="101", close="102", high="102.0", low="100.8"),  # fill 101.3 -> turn spent
        bar(5, open_="102", close="99.5", high="102.1", low="99.0"),  # closes BELOW, trade open
        bar(6, open_="99.5", close="104", high="104.2", low="99.4"),  # crosses back: new turn
        bar(7, open_="104", close="108", high="108.5", low="103.9"),  # target 108.10 -> flat, arms
        bar(8, open_="108", close="109", high="109.5", low="107.9"),  # breaks 108.5 -> fill
        bar(9, open_="109", close="110", high="110.5", low="108.9"),
    ]
    result = _run(candles)

    entries = _entries(result)
    assert len(entries) == 2
    assert entries[0].price == Decimal("101.3")
    # the second turn's reference is bar 7, the very bar the first trade closed on
    assert entries[1].price == Decimal("108.5")
    assert entries[1].order.stop_loss == Decimal("103.9")
    assert entries[1].time == candles[8].time
    [first_exit] = [f for f in result.fills if f.order.intent is SignalKind.EXIT]
    assert first_exit.order.reason == "tp"
    assert first_exit.time == candles[7].time


def test_a_break_that_fills_before_a_close_back_across_wins_the_race() -> None:
    """The cancel protects the *next* bar, not the bar it is decided on. Bar 3 arms at 101.3; bar 4
    breaks 101.3 intrabar (high 101.5) **and** closes back below the MME3. The break physically
    happened before the close, so the broker fills it in step 1; the strategy only learns to cancel
    at the close, too late. The fill stands — that is the honest reading, and it is pinned here so a
    refactor cannot quietly make the close-below win a race it never should."""
    candles = [
        *_SEED,
        bar(3, open_="98", close="101", high="101.3", low="97.9"),  # arm trigger 101.3
        bar(
            4, open_="101", close="98", high="101.5", low="97.8"
        ),  # breaks 101.3, then closes below
    ]
    result = _run(candles)
    [entry] = _entries(result)
    assert entry.price == Decimal("101.3")
    assert entry.time == candles[4].time


def test_a_short_rearms_to_the_latest_bar() -> None:
    """The short mirror of the rearm, so the `SHORT` branch is not trusted on symmetry alone. Bar 3
    closes below the MME3 and arms at its low 98.7; bar 4 closes below too but its low 98.8 never
    reaches 98.7, so the reference moves down to bar 4, trigger 98.8 and stop at bar 4's high 99.1.
    Bar 5 breaks 98.8, and the short fills at 98.8, not 98.7."""
    candles = [
        bar(0, open_="100", close="100", high="100.5", low="99.5"),
        bar(1, open_="100", close="101", high="101.2", low="99.8"),
        bar(2, open_="101", close="102", high="102.5", low="100.8"),  # MME3 seeds at 101
        bar(3, open_="102", close="99", high="102.1", low="98.7"),  # cross down; trigger 98.7
        bar(4, open_="99", close="99", high="99.1", low="98.8"),  # rearm; trigger 98.8, stop 99.1
        bar(5, open_="99", close="96", high="99.05", low="95.5"),  # breaks 98.8 -> fill
    ]
    result = _run(candles, side=Side.SHORT)
    [entry] = _entries(result)
    assert entry.price == Decimal("98.8")
    assert entry.order.stop_loss == Decimal("99.1")


# --------------------------------------------------------------------------- #
# How the strategy learns a fill happened (ADR-0015)                            #
# --------------------------------------------------------------------------- #


_FILL_CANDLES = [
    *_SEED,
    bar(3, open_="98", close="101", high="101.3", low="97.9"),  # arm: mme9-...T0300-1
    bar(4, open_="101", close="102", high="102.2", low="100.8"),  # on-side, has range
]


def test_only_a_fill_carrying_the_armed_name_spends_the_turn() -> None:
    """The fill is matched by name, not merely counted.

    Bar 4 shows a fill belonging to someone else — a hedge, a manual trade, another strategy on
    the same account in live. It is not this setup's order, so the turn is untouched and bar 4
    does what an unfilled on-side bar always does: withdraws bar 3's order and rearms at its own
    high. Matching on "any fill at all" would spend a turn the setup never traded, and the entry
    would simply never happen — silence that reads exactly like a signal that never came.
    """
    strategy = Mme9BreakoutStrategy(period=3)
    signals = _drive(
        strategy,
        _FILL_CANDLES,
        fills_on={4: [_fill_named(_FILL_CANDLES[4], "someone-elses-order-1")]},
    )

    [armed] = signals[3]
    withdraw, rearm = signals[4]
    assert withdraw.kind is SignalKind.CANCEL
    assert withdraw.client_id == armed.client_id
    assert rearm.kind is SignalKind.ENTRY
    assert rearm.stop_price == Decimal("102.2")
    # The reference price is the close that decided, on both — the price the setup was read at.
    assert armed.reference_price == Decimal("101")
    assert rearm.reference_price == Decimal("102")


def test_the_setups_own_fill_spends_the_turn_and_forgets_the_order() -> None:
    """The same bar, with the fill carrying *this* strategy's name: the turn is spent, so bar 4
    arms nothing. And it emits no cancel either — the armed name is dropped, because an order the
    market already consumed is not resting to be withdrawn, and cancelling a consumed name in live
    is at best a wasted round trip and at worst a race against the position it just opened."""
    strategy = Mme9BreakoutStrategy(period=3)
    first = _drive(strategy, _FILL_CANDLES[:4])
    [armed] = first[3]
    assert armed.client_id is not None

    candles = [*_FILL_CANDLES, bar(5, open_="102", close="97", high="102.1", low="96.9")]
    replay = Mme9BreakoutStrategy(period=3)
    signals = _drive(replay, candles, fills_on={4: [_fill_named(candles[4], armed.client_id)]})

    assert signals[4] == []  # turn spent: nothing arms
    # Bar 5 closes back across the average — the one bar that *would* withdraw a resting order.
    # It emits nothing, which is how we know the consumed name was dropped and not merely ignored.
    assert signals[5] == []


def test_an_open_position_is_the_fallback_sign_of_a_fill() -> None:
    """The second channel, for the fill this strategy was never shown.

    The `BacktestBroker` always reports the bar's fills, so this path is unreachable here — but a
    live adapter reconnecting, or one that reports positions before it reports fills, would show
    an open position with no fill to go with it. Bar 4 has the position and no fills at all; the
    turn must still be spent, which bar 5 proves by staying silent while flat and on-side.

    Bar 5 is where the assertion has to live: bar 4 is silent either way, because an open position
    arms nothing regardless. Without the fallback, bar 5 would find the old name still armed and
    cancel-and-rearm it — re-entering a turn the account had already traded.
    """
    candles = [*_FILL_CANDLES, bar(5, open_="102", close="103", high="103.5", low="101.9")]
    signals = _drive(Mme9BreakoutStrategy(period=3), candles, position_on=frozenset({4}))

    assert len(signals[3]) == 1  # armed
    assert signals[4] == []  # a position owns the bar
    assert signals[5] == []  # flat and on-side again, but the turn is spent


# --------------------------------------------------------------------------- #
# Reading the average                                                          #
# --------------------------------------------------------------------------- #


def test_the_average_keeps_moving_while_a_trade_is_open() -> None:
    """The MME9 is fed by every bar, not only the bars the strategy acts on.

    An average that skips the bars of an open trade is a different average, and it decides
    different turns. Here bars 3 and 4 close at 110 with a position open, dragging the MME3 to
    107.25; bar 5 closes 105 — **below** that, so the turn is over and nothing arms. An average
    frozen at 99 through the trade would put bar 5's average at 102, call 105 on-side, and arm a
    trade the real average says does not exist.
    """
    candles = [
        *_SEED,
        bar(3, open_="98", close="110", high="110.5", low="97.9"),
        bar(4, open_="110", close="110", high="110.5", low="109.5"),
        bar(5, open_="110", close="105", high="110.2", low="104.5"),  # MME3 106.125 -> off-side
    ]
    signals = _drive(Mme9BreakoutStrategy(period=3), candles, position_on=frozenset({3, 4}))
    assert signals[5] == []


def test_an_open_position_arms_nothing_even_on_a_fresh_cross() -> None:
    """The position guard, isolated from the turn machinery that usually hides it.

    In a backtest an open position always implies a spent turn, because the fill that opened it
    spent one — so the guard looks redundant. It is not: a live account can hold a position this
    strategy never opened (a manual trade, a reconnect that replays state), and then `_spent` is
    `False` while a trade is running. Bars 3 and 4 are exactly that — a textbook cross up, on a
    strategy that has armed nothing — and they must stay silent, because a second entry stacked
    on an open trade doubles the risk the sizing was computed for.
    """
    candles = [
        *_SEED,
        bar(3, open_="98", close="110", high="110.5", low="97.9"),  # a cross up, position open
        bar(4, open_="110", close="110", high="110.5", low="109.5"),  # still on-side, still open
    ]
    signals = _drive(Mme9BreakoutStrategy(period=3), candles, position_on=frozenset({3, 4}))
    assert signals == [[], [], [], [], []]


def test_a_bar_closing_exactly_on_the_average_has_not_crossed_it() -> None:
    """Rule 1 is 'closes *above* the average', and the comparison is strict on purpose. Three flat
    bars leave the MME3 at exactly 100.0 and bar 3 closes at exactly 100 — it has not turned
    anything, so it is no reference, and bar 4 (which does close above) is the first one. Read
    non-strictly, bar 3 would arm at its high of 101.5 and bar 4 would fill it."""
    candles = [
        bar(0, open_="100", close="100", high="100.5", low="99.5"),
        bar(1, open_="100", close="100", high="100.4", low="99.6"),
        bar(2, open_="100", close="100", high="100.3", low="99.7"),  # MME3 seeds at exactly 100
        bar(3, open_="100", close="100", high="101.5", low="98.5"),  # closes ON the average
        bar(4, open_="100", close="103", high="103.5", low="99.9"),  # would break 101.5 if armed
    ]
    result = _run(candles)
    assert result.fills == ()


def test_an_order_the_gap_discarded_does_not_spend_the_turn() -> None:
    """A discarded order is not a trade, and only a trade spends a turn.

    What discards it is the **half-R slip ceiling** (ADR-0016), not the gap-through-the-stop guard
    that protects a limit. A stop entry fills *away* from its own stop, so it can never open past
    it — `_survives_the_gap` is a no-op here (110 > 97.9) and the order is thrown out by
    `_survives_the_slip`: the trade was sized for the reference bar's range of 3.4, and bar 4's
    open of 110 is 8.7 away from the trigger at 101.3, well past the 1.7 the ceiling allows. Filled
    anyway, the position would carry two and a half times the risk the risk manager agreed to.

    No fill means no `_spent`, and bar 4 — on-side, with a range — is simply the next reference.
    Spending the turn on a discard would lose the setup entirely; leaving the old name armed would
    leave a ghost order behind it. This test is therefore sensitive to `_MAX_ENTRY_SLIP`.
    """
    candles = [
        *_SEED,
        bar(3, open_="98", close="101", high="101.3", low="97.9"),  # arm: trigger 101.3, stop 97.9
        bar(4, open_="110", close="112", high="112.5", low="109.9"),  # gaps past both -> discarded
        bar(5, open_="112", close="113", high="113.5", low="111.9"),  # breaks bar 4's 112.5
    ]
    result = _run(candles)
    [entry] = _entries(result)
    assert entry.price == Decimal("112.5")  # bar 4's high, not bar 3's 101.3
    assert entry.order.stop_loss == Decimal("109.9")
    assert entry.time == candles[5].time


# --------------------------------------------------------------------------- #
# Determinism (AGENTS.md §5)                                                   #
# --------------------------------------------------------------------------- #


def test_the_same_candles_always_produce_the_same_run() -> None:
    """Invariant #2, pinned for this strategy rather than assumed from the loop's own tests: two
    runs over the same bars agree fill for fill, price for price. A `set` iteration or a clock
    sneaking into the arming path would show up here and nowhere else."""
    candles = [
        *_SEED,
        bar(3, open_="98", close="101", high="101.3", low="97.9"),
        bar(4, open_="101", close="102", high="102.0", low="100.8"),
        bar(5, open_="102", close="99", high="102.1", low="98.5"),
        bar(6, open_="99", close="104", high="104.2", low="98.9"),
        bar(7, open_="104", close="106", high="106.5", low="103.9"),
    ]
    first, second = _run(candles), _run(candles)
    assert [(f.time, f.price, f.order.client_id, f.order.stop_loss) for f in first.fills] == [
        (f.time, f.price, f.order.client_id, f.order.stop_loss) for f in second.fills
    ]
    assert first.final_account == second.final_account


@given(candles=_random_candles(), long=st.booleans())
@settings(max_examples=200)
def test_a_turn_never_gives_more_than_one_trade(candles: list[Candle], long: bool) -> None:
    """The property behind every one-trade-per-turn golden: whatever the market does, an entry
    costs a turn, and a turn is only reopened by a bar closing on the wrong side of the MME9.

    Asserted **between consecutive entries**, not as a total count. A count is satisfied by any
    run with enough wrong-side closes *somewhere*, which is precisely the shape a martingale takes:
    two entries back to back inside one trend, paid for by boundaries that happened elsewhere in
    the stream. Pairing each entry with a boundary in its own window is what makes the property
    bite, and the window is `[previous fill, this fill)` — a bar can fill an order in step 1 and
    close back across the average at its own close, which ends the turn on the very bar it began.
    """
    side = Side.LONG if long else Side.SHORT
    result = _run(candles, side=side)
    entries = _entries(result)

    # The same MME3 the strategy saw, recomputed here to find the turn boundaries.
    with localcontext(ENGINE_CONTEXT):
        ema = EMA(period=3)
        wrong_side_closes = []
        for candle in candles:
            ema.update(candle)
            average = ema.value()
            if average is None:
                continue
            on_side = candle.close > average if long else candle.close < average
            if not on_side:
                wrong_side_closes.append(candle.time)

    assert len(entries) <= len(wrong_side_closes) + 1
    # Every entry but the first is paid for by a boundary of its own.
    for previous, entry in pairwise(entries):
        assert any(previous.time <= t < entry.time for t in wrong_side_closes), (
            f"entry at {entry.time} follows {previous.time} with no wrong-side close between them"
        )
    # times strictly increase: no two fills share a bar, and none goes backwards
    times = [fill.time for fill in entries]
    assert times == sorted(set(times))


def test_the_period_must_be_a_real_average() -> None:
    with pytest.raises(ValueError, match="MME period"):
        Mme9BreakoutStrategy(period=0)


def test_the_stop_buffer_is_a_magnitude() -> None:
    with pytest.raises(ValueError, match="stop buffer"):
        Mme9BreakoutStrategy(stop_buffer_ticks=-1)

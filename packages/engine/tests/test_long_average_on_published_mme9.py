"""His long-average filter on the published MME9 family: the 9.1, the 9.2/9.3 and the 9.4.

The filter itself — and its three rules — is pinned in `test_long_average_filter.py`, on the two
setups it was dictated for. This file holds the same three rules on the three published setups,
because each one gates on its **own line** of its own `on_bar`, and a filter that reaches the
class through the factory but is checked on the wrong price, or one line too late, passes every
routing test there is:

* **the entry price is what is compared, not the close** — every "allowed" fixture below is built
  so that the bar that arms *closes under* the long average and *breaks over* it;
* **a blocked bar places nothing and withdraws nothing** — the 9.2 is the one of the three that
  re-prices a resting order, so it is the one where a gate past the withdrawal would show;
* **a refused 9.1 or 9.4 is gone** — their arming is an event, not a state, so nothing is left to
  come back to on the next bar. That is recorded here as behaviour, not as a bug.

Each fixture opens with a flat shelf at a higher price, which is what lifts the long average above
the setup's own and lets one period separate the two readings. ⚠️ **Every number here came from
a probe of the real setups**, not from arithmetic on paper.
"""

from decimal import Decimal, localcontext

import pytest

from tradeforge_engine.domain import Candle, Context, Position, Side, Signal, SignalKind
from tradeforge_engine.indicators import EMA
from tradeforge_engine.loop import ENGINE_CONTEXT
from tradeforge_engine.swing import (
    Mme9FailedTurnStrategy,
    Mme9PullbackStrategy,
    Mme9TurnStrategy,
)
from tradeforge_engine.testing import AAPL, ImmediateFillBroker, bar

_ACCOUNT = ImmediateFillBroker(instrument=AAPL).account()
_MIRROR_AXIS = Decimal(200)

_Published = Mme9TurnStrategy | Mme9PullbackStrategy | Mme9FailedTurnStrategy


def _rows(rows: list[tuple[str, str, str]]) -> list[Candle]:
    """Bars from `(close, high, low)`, opening at their close so no stop is refused for gapping."""
    return [
        bar(index, open_=close, close=close, high=high, low=low)
        for index, (close, high, low) in enumerate(rows)
    ]


def _shelf(price: int, bars: int) -> list[tuple[str, str, str]]:
    return [(str(price), str(price + 0.5), str(price - 0.5))] * bars


def _mirror(candles: list[Candle]) -> list[Candle]:
    """Reflect about a price: the buy scenario becomes its sell twin, extremes swapped."""
    return [
        Candle(
            time=candle.time,
            open=_MIRROR_AXIS - candle.open,
            high=_MIRROR_AXIS - candle.low,
            low=_MIRROR_AXIS - candle.high,
            close=_MIRROR_AXIS - candle.close,
        )
        for candle in candles
    ]


def _oriented(candles: list[Candle], side: Side) -> list[Candle]:
    return candles if side is Side.LONG else _mirror(candles)


def _drive(
    strategy: _Published,
    candles: list[Candle],
    *,
    position_on: frozenset[int] = frozenset(),
    held: Position | None = None,
) -> list[list[Signal]]:
    """Feed candles one at a time; what each bar produced, the silent ones included."""
    out: list[list[Signal]] = []
    with localcontext(ENGINE_CONTEXT):
        for index, candle in enumerate(candles):
            context = Context(
                candle=candle,
                instrument=AAPL,
                account=_ACCOUNT,
                position=held if index in position_on else None,
            )
            out.append(list(strategy.on_bar(context)))
    return out


def _entries(signals: list[list[Signal]]) -> dict[int, Signal]:
    return {
        index: signal
        for index, bar_signals in enumerate(signals)
        for signal in bar_signals
        if signal.kind is SignalKind.ENTRY
    }


def _is_between(close: Decimal, long_average: Decimal, entry: Decimal, side: Side) -> bool:
    """The long average strictly between the close and the entry, on the side's own terms."""
    if side is Side.LONG:
        return close < long_average < entry
    return entry < long_average < close


# --------------------------------------------------------------------------- #
# Fixtures                                                                      #
# --------------------------------------------------------------------------- #

# The 9.1: a shelf at 113, a fall to 95 that bends the MME3 down, and bar 12 bending it back up.
# Bar 12 closes at 105 and breaks at 106; a twelve-period long average reads 105.21 there.
_TURN = [
    (close, str(float(close) + 1), str(float(close) - 1.5))
    for close in ["100", "99", "98", "97", "96", "95", "105", "106", "105"]
]
_TURN_UNDER_THE_SHELF = _rows([*_shelf(113, 6), *_TURN])
_TURN_UNDER_A_HIGHER_SHELF = _rows([*_shelf(116, 6), *_TURN])  # the long average at 106.48
_TURN_BAR = 12
_TURN_LONG = 12

# The 9.2: a shelf at 107, a rising leg whose best close is bar 6, and bar 7 correcting under it.
# Bar 7 closes at 104 and breaks at 104.6; an eight-period long average reads 104.125 there.
_LEG = [
    ("100", "100.5", "99.5"),
    ("101", "101.5", "100.5"),
    ("102", "102.5", "101.5"),
    ("105", "105.5", "104.5"),
]
_CORRECTION = ("104", "104.6", "102.0")
_PULLBACK_UNDER_THE_SHELF = _rows([*_shelf(107, 3), *_LEG, _CORRECTION])
_PULLBACK_UNDER_A_HIGHER_SHELF = _rows([*_shelf(110, 3), *_LEG, _CORRECTION])  # long at 105.25
_PULLBACK_BAR = 7
_PULLBACK_LONG = 8

# Two more corrections after bar 7, for the order that must survive a blocked bar. Bar 8 is still
# a correction — without the filter it re-prices the order rather than withdrawing it, so the
# setup has not come apart — and its break at 104.0 is under the long average of 104.075. Bar 9
# breaks at 104.9 over 104.147 and re-prices.
_BLOCKED_CORRECTION = ("103.9", "104.0", "103.0")
_CLEARING_CORRECTION = ("104.4", "104.9", "103.9")
_PULLBACK_THAT_RESTS = _rows(
    [*_shelf(107, 3), *_LEG, _CORRECTION, _BLOCKED_CORRECTION, _CLEARING_CORRECTION]
)

# The 9.4: a shelf at 110, a rising MME3, the one-bar dip on bar 7 and its recovery on bar 8.
# Bar 8 closes at 105 and breaks at 105.8; an eight-period long average reads 105.097 there. The
# bar after it runs on, to show that nothing comes back once the filter has said no.
_RISING = [
    ("100", "100.5", "99.5"),
    ("101", "101.5", "100.5"),
    ("102", "102.5", "101.5"),
    ("106", "106.5", "105.0"),
]
_FAILURE = ("102", "104.0", "101.0")
_RECOVERY = ("105", "105.8", "101.5")
_RUNS_ON = ("106", "106.5", "105")
_FAILED_TURN_UNDER_THE_SHELF = _rows([*_shelf(110, 3), *_RISING, _FAILURE, _RECOVERY, _RUNS_ON])
_FAILED_TURN_UNDER_A_HIGHER_SHELF = _rows(  # the long average at 105.97
    [*_shelf(113, 3), *_RISING, _FAILURE, _RECOVERY, _RUNS_ON]
)
_FAILED_TURN_BAR = 8
_FAILED_TURN_LONG = 8


# --------------------------------------------------------------------------- #
# His rule: the price the order would enter at                                  #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
@pytest.mark.parametrize(
    ("make", "candles", "at", "extra_context"),
    [
        pytest.param(
            lambda side, long: Mme9TurnStrategy(side=side, period=3, long_average_period=long),
            _TURN_UNDER_THE_SHELF,
            (_TURN_BAR, _TURN_LONG),
            set[str](),
            id="9.1",
        ),
        pytest.param(
            lambda side, long: Mme9PullbackStrategy(
                side=side, period=3, corrections=1, long_average_period=long
            ),
            _PULLBACK_UNDER_THE_SHELF,
            (_PULLBACK_BAR, _PULLBACK_LONG),
            {"reference_close"},
            id="9.2",
        ),
        pytest.param(
            lambda side, long: Mme9FailedTurnStrategy(
                side=side, period=3, long_average_period=long
            ),
            _FAILED_TURN_UNDER_THE_SHELF,
            (_FAILED_TURN_BAR, _FAILED_TURN_LONG),
            {"average_at_failure"},
            id="9.4",
        ),
    ],
)
def test_the_bar_that_arms_may_close_beyond_the_long_average_it_breaks_across(
    make: object,
    candles: list[Candle],
    at: tuple[int, int],
    extra_context: set[str],
    side: Side,
) -> None:
    """⚠️ *"se a entrada ocorre acima da média longa já conta neste caso mesmo o fechamento sendo
    abaixo"*. On each of the three, the arming bar closes on the wrong side of the long average and
    enters on the right one — so a gate written on the close would refuse exactly this entry, and
    the backtest would come out quietly different with no number looking wrong.

    The sell side is the reflection about 200, where the close sits *above* the long average and
    the entry below it, so a sign error cannot pass by symmetry.

    The entry carries the long average both ways it is read: as a number next to its own context,
    and as a second curve beside the setup's average.
    """
    assert callable(make)
    index, long_period = at
    oriented = _oriented(candles, side)
    signals = _drive(make(side, long_period), oriented)
    entries = _entries(signals)

    assert list(entries) == [index]
    entry = entries[index]
    assert entry.side is side
    assert entry.stop_price is not None
    assert entry.context is not None
    assert set(entry.context) == {"average", "long_average", *extra_context}
    long_average = entry.context["long_average"]
    assert long_average is not None
    assert _is_between(oriented[index].close, long_average, entry.stop_price, side)
    assert [series.label for series in entry.series] == ["average", f"long EMA {long_period}"]


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
@pytest.mark.parametrize(
    ("make", "candles"),
    [
        pytest.param(
            lambda side: Mme9TurnStrategy(side=side, period=3, long_average_period=_TURN_LONG),
            _TURN_UNDER_A_HIGHER_SHELF,
            id="9.1",
        ),
        pytest.param(
            lambda side: Mme9PullbackStrategy(
                side=side, period=3, corrections=1, long_average_period=_PULLBACK_LONG
            ),
            _PULLBACK_UNDER_A_HIGHER_SHELF,
            id="9.2",
        ),
        pytest.param(
            lambda side: Mme9FailedTurnStrategy(
                side=side, period=3, long_average_period=_FAILED_TURN_LONG
            ),
            _FAILED_TURN_UNDER_A_HIGHER_SHELF,
            id="9.4",
        ),
    ],
)
def test_an_entry_on_the_wrong_side_of_the_long_average_places_nothing(
    make: object, candles: list[Candle], side: Side
) -> None:
    """The same pattern under a higher shelf, where the long average sits beyond the break too.
    The run arms nothing at all — and for the 9.1 and the 9.4 that includes the bars after it:
    their arming is an event, so a refused turn or a refused failure is not waiting to be retried.
    """
    assert callable(make)
    oriented = _oriented(candles, side)
    assert _entries(_drive(make(side), oriented)) == {}


@pytest.mark.parametrize(
    ("unfiltered", "filtered", "candles", "index"),
    [
        pytest.param(
            Mme9TurnStrategy(period=3),
            Mme9TurnStrategy(period=3, long_average_period=_TURN_LONG),
            _TURN_UNDER_A_HIGHER_SHELF,
            _TURN_BAR,
            id="9.1",
        ),
        pytest.param(
            Mme9PullbackStrategy(period=3, corrections=1),
            Mme9PullbackStrategy(period=3, corrections=1, long_average_period=_PULLBACK_LONG),
            _PULLBACK_UNDER_A_HIGHER_SHELF,
            _PULLBACK_BAR,
            id="9.2",
        ),
        pytest.param(
            Mme9FailedTurnStrategy(period=3),
            Mme9FailedTurnStrategy(period=3, long_average_period=_FAILED_TURN_LONG),
            _FAILED_TURN_UNDER_A_HIGHER_SHELF,
            _FAILED_TURN_BAR,
            id="9.4",
        ),
    ],
)
def test_the_refused_fixtures_do_arm_without_the_filter(
    unfiltered: _Published, filtered: _Published, candles: list[Candle], index: int
) -> None:
    """A guard on the fixtures rather than on the code: a refusal proves nothing unless the same
    bars arm when the filter is off, and a shelf that broke the pattern would pass the test above
    for the wrong reason. And the long average really is past the entry there."""
    [entry] = _entries(_drive(unfiltered, candles)).values()
    _drive(filtered, candles[: index + 1])
    long_average = filtered._long
    assert long_average is not None
    value = long_average.value()
    assert value is not None
    assert entry.stop_price is not None
    assert value > entry.stop_price


# --------------------------------------------------------------------------- #
# It gates placing; it never withdraws                                          #
# --------------------------------------------------------------------------- #


def test_a_blocked_correction_leaves_the_92_s_resting_order_where_it_was() -> None:
    """⚠️ *"Ela fica, só retira se o setup desconfigurar."* The 9.2 is the published setup that
    re-prices a resting order to the newest bar of the correction, so it is where a gate placed one
    line too low — past the withdrawal — would take an order back.

    Bar 7 arms at 104.6. Bar 8 is still a correction, and without the filter it re-prices to 104.0;
    with it, that break is under the long average and the bar sends **nothing**. Bar 9 then clears
    and replaces the order — and the cancel it sends names bar 7's, which is the proof the order
    was still resting. Had the filter withdrawn it on bar 8, there would be nothing to name.
    """
    unfiltered = _drive(Mme9PullbackStrategy(period=3, corrections=1), _PULLBACK_THAT_RESTS)
    assert [signal.kind for signal in unfiltered[8]] == [SignalKind.CANCEL, SignalKind.ENTRY]

    signals = _drive(
        Mme9PullbackStrategy(period=3, corrections=1, long_average_period=_PULLBACK_LONG),
        _PULLBACK_THAT_RESTS,
    )
    armed = _entries(signals)[_PULLBACK_BAR]
    assert str(armed.stop_price) == "104.6"

    assert signals[8] == []
    [cancelled, replaced] = signals[9]
    assert cancelled.kind is SignalKind.CANCEL
    assert cancelled.client_id == armed.client_id
    assert replaced.kind is SignalKind.ENTRY
    assert str(replaced.stop_price) == "104.9"


@pytest.mark.parametrize(
    ("strategy", "candles"),
    [
        pytest.param(
            Mme9TurnStrategy(period=3, long_average_period=4), _TURN_UNDER_THE_SHELF, id="9.1"
        ),
        pytest.param(
            Mme9PullbackStrategy(period=3, corrections=1, long_average_period=4),
            _PULLBACK_THAT_RESTS,
            id="9.2",
        ),
        pytest.param(
            Mme9FailedTurnStrategy(period=3, long_average_period=4),
            _FAILED_TURN_UNDER_THE_SHELF,
            id="9.4",
        ),
    ],
)
def test_the_long_average_keeps_counting_while_a_trade_is_open(
    strategy: _Published, candles: list[Candle]
) -> None:
    """⚠️ The bars a trade is open on return early — they are the conduction's, not the filter's —
    and the filter's average has to be fed on them all the same. One skipped there freezes while
    the market runs, and the next entry after the stop is judged against a stale line: a 9.2 long
    on a 200-period filter through a forty-bar rally would come out of it allowing corrections
    the real average refuses, and the backtest would simply trade more.

    Held on bars 3 to 7, the middle of every fixture; the reference is the same average fed
    every bar with no setup around it.
    """
    held = Position(
        symbol=AAPL.symbol,
        side=Side.LONG,
        volume=Decimal(1),
        entry_price=candles[3].close,
        entry_time=candles[3].time,
    )
    _drive(strategy, candles, position_on=frozenset(range(3, 8)), held=held)

    reference = EMA(period=4, source="close")
    with localcontext(ENGINE_CONTEXT):
        for candle in candles:
            reference.update(candle)
    long_average = strategy._long
    assert long_average is not None
    assert long_average.value() == reference.value()


# --------------------------------------------------------------------------- #
# Warming up, charting, and off by default                                      #
# --------------------------------------------------------------------------- #


def test_nothing_arms_while_the_long_average_is_still_warming_up() -> None:
    """The 9.1 turns on bar 6 of the bare fall, and an eight-period average has no value until bar
    7. With nothing to be above, the filter refuses — the run arms nothing rather than trading its
    first bars unfiltered — and the turn does not come back once the average is warm."""
    candles = _rows(_TURN)
    assert list(_entries(_drive(Mme9TurnStrategy(period=3), candles))) == [6]
    assert _entries(_drive(Mme9TurnStrategy(period=3, long_average_period=8), candles)) == {}


@pytest.mark.parametrize("cls", [Mme9TurnStrategy, Mme9PullbackStrategy, Mme9FailedTurnStrategy])
def test_the_chart_is_offered_the_long_average_only_when_the_filter_is_on(
    cls: type[_Published],
) -> None:
    """A chart drawing only the MME9 would show entries being skipped with nothing on it to say
    why. And the label keeps a filter of the setup's own period a second curve."""
    assert list(cls(period=9).overlays()) == ["EMA 9"]
    assert list(cls(period=9, long_average_period=9).overlays()) == ["EMA 9", "long EMA 9"]


@pytest.mark.parametrize("cls", [Mme9TurnStrategy, Mme9PullbackStrategy, Mme9FailedTurnStrategy])
def test_the_published_family_defaults_to_no_filter(cls: type[_Published]) -> None:
    """Every recorded result predates this parameter, so its absence has to change nothing —
    including the entry's record, where an absent key says something a present one does not."""
    assert cls(period=3)._long is None


def test_an_unfiltered_entry_carries_no_long_average() -> None:
    [entry] = _entries(
        _drive(Mme9FailedTurnStrategy(period=3), _FAILED_TURN_UNDER_THE_SHELF)
    ).values()
    assert entry.context is not None
    assert "long_average" not in entry.context
    assert [series.label for series in entry.series] == ["average"]


def test_the_bars_line_up_with_the_numbers_they_are_named_by() -> None:
    """A guard on the fixtures: the tests above index by bar number."""
    assert _TURN_UNDER_THE_SHELF[_TURN_BAR].close == Decimal(105)
    assert _PULLBACK_UNDER_THE_SHELF[_PULLBACK_BAR].close == Decimal(104)
    assert _FAILED_TURN_UNDER_THE_SHELF[_FAILED_TURN_BAR].close == Decimal(105)

"""The published 9.4: the average dips for exactly one bar, comes straight back, and that is it.

The pattern is two bars and every rule is about their relationship, so the fixtures are built to
make each of those relationships fail on its own:

* the recovery has to be the **next** bar — two bars down is a 9.1 later, not a 9.4 here;
* the recovery must not take the failure bar's low, because the literature says that bar is the
  opposite setup;
* the stop comes from the **failure** bar, which in an ordinary pattern is deeper than the recovery
  bar — so the two are different numbers and a test can tell them apart.

Golden numbers come from feeding these exact bars to the engine.
"""

from decimal import Decimal, localcontext

from tradeforge_engine.backtest_broker import BacktestBroker
from tradeforge_engine.domain import (
    Candle,
    Context,
    Position,
    Side,
    Signal,
    SignalKind,
)
from tradeforge_engine.loop import ENGINE_CONTEXT, RunResult, run
from tradeforge_engine.risk import PercentRiskManager
from tradeforge_engine.swing import Mme9FailedTurnStrategy
from tradeforge_engine.testing import AAPL, HOUR, ImmediateFillBroker, bar

_ACCOUNT = ImmediateFillBroker(instrument=AAPL).account()

# A rising MME3. Bar 3 leaves it at 103.5 and climbing, which is what the failure bar then bends.
_RISING: list[tuple[str, str, str]] = [
    ("100", "100.5", "99.5"),
    ("101", "101.5", "100.5"),
    ("102", "102.5", "101.5"),
    ("106", "106.5", "105.0"),
]

# ⚠️ The failure bar's low (101.0) is **below** the recovery bar's (101.5), which is what makes
# "the stop is the failure bar" a different number from "the stop is the bar that triggers".
_FAILURE: tuple[str, str, str] = ("102", "104.0", "101.0")
_RECOVERY: tuple[str, str, str] = ("105", "105.8", "101.5")


def _candles(rows: list[tuple[str, str, str]]) -> list[Candle]:
    """Bars from `(close, high, low)`, opening at their close so no fill is refused for gapping."""
    return [
        bar(index, open_=close, close=close, high=high, low=low)
        for index, (close, high, low) in enumerate(rows)
    ]


def _drive(
    strategy: Mme9FailedTurnStrategy,
    candles: list[Candle],
    *,
    position_on: frozenset[int] = frozenset(),
    held: Position | None = None,
) -> list[list[Signal]]:
    out: list[list[Signal]] = []
    with localcontext(ENGINE_CONTEXT):
        for index, candle in enumerate(candles):
            context = Context(
                candle=candle,
                instrument=AAPL,
                account=_ACCOUNT,
                position=held if index in position_on else None,
                fills=(),
            )
            out.append(list(strategy.on_bar(context)))
    return out


def _kinds(signals: list[list[Signal]]) -> list[list[str]]:
    return [[signal.kind.name for signal in bars] for bars in signals]


def _levels(signal: Signal) -> tuple[str, str]:
    return str(signal.stop_price), str(signal.stop_loss)


def test_the_dip_and_its_recovery_arm_at_the_recovery_bars_high() -> None:
    signals = _drive(
        Mme9FailedTurnStrategy(side=Side.LONG, period=3),
        _candles([*_RISING, _FAILURE, _RECOVERY]),
    )

    # The failure bar arms nothing on its own: the pattern is not complete until the next bar.
    assert _kinds(signals) == [[], [], [], [], [], ["ENTRY"]]
    (entry,) = signals[5]
    # Triggers at the recovery bar's high, protected under the **failure** bar's low — 101.00, not
    # the 101.5 of the bar it entered on.
    assert _levels(entry) == ("105.8", "101.00")
    assert entry.reason == "entry.mme9fail"
    # The average at the bottom of the dip is the one number the record would otherwise lose.
    context = entry.context
    assert context is not None
    assert context["average_at_failure"] == Decimal("102.75")
    assert context["average"] == Decimal("103.875")


def test_a_recovery_that_takes_the_failure_bars_low_is_the_opposite_setup() -> None:
    """Rule 4, and it is arming rather than cancelling: nothing is ever placed.

    The literature is explicit that a recovery bar breaking the failure bar's low is *"um Setup 9.1
    de VENDA"*. Arming here would be buying the break of a bar that has just made a lower low —
    a reversal traded as a continuation, and the backtest would only show it as a worse average.

    ⚠️ **Touched is not violated, and the second half of this test is what says so.** A recovery bar
    that comes back to the failure bar's low *exactly* is a double bottom on one level, which is the
    archetypal shape this setup produces — the correction retesting its own low. It arms. Reading
    the rule as "must not reach" instead of "must not break" deletes that whole family of trades,
    and on a coarse tick size it is a slice of the signal universe rather than a corner of it.
    """
    violated = _drive(
        Mme9FailedTurnStrategy(side=Side.LONG, period=3),
        # Same recovery as the passing case, except its low reaches 100.5 — under the 101.0.
        _candles([*_RISING, _FAILURE, ("105", "105.8", "100.5"), ("105.5", "106.0", "104.0")]),
    )
    assert _kinds(violated) == [[], [], [], [], [], [], []]

    touched = _drive(
        Mme9FailedTurnStrategy(side=Side.LONG, period=3),
        _candles([*_RISING, _FAILURE, ("105", "105.8", "101.0")]),
    )
    assert _kinds(touched) == [[], [], [], [], [], ["ENTRY"]]
    assert _levels(touched[5][0]) == ("105.8", "101.00")


def test_the_recovery_has_to_be_the_very_next_bar() -> None:
    """Two bars down and this setup is over; what turns the line back up later is a 9.1.

    The separator against the obvious implementation — "remember the failure bar until the average
    recovers" — which would arm on bar 6 here, with a stop reaching back to a bar two places
    behind it and a trigger far above the pattern it claims to be.
    """
    signals = _drive(
        Mme9FailedTurnStrategy(side=Side.LONG, period=3),
        _candles(
            [
                *_RISING,
                _FAILURE,
                ("101", "102.0", "100.0"),  # still falling: the pattern's one chance is gone
                ("105", "105.8", "101.5"),  # turns it back up, but too late to be a 9.4
            ]
        ),
    )

    assert _kinds(signals) == [[], [], [], [], [], [], []]


def test_the_order_does_not_chase_the_bars_after_the_recovery() -> None:
    """Frozen, like the published 9.1 and unlike the 9.2/9.3 — bar 6 changes nothing."""
    signals = _drive(
        Mme9FailedTurnStrategy(side=Side.LONG, period=3),
        _candles([*_RISING, _FAILURE, _RECOVERY, ("105.5", "106.0", "104.0")]),
    )

    assert _kinds(signals)[5:] == [["ENTRY"], []]


def test_the_bar_that_bends_the_line_again_both_cancels_and_starts_the_next_pattern() -> None:
    """One event, two jobs — and a reading that does only the first cannot re-arm afterwards.

    Bar 6 bends the average down: it withdraws the order bar 5 placed **and** becomes the failure
    bar of a fresh 9.4, which bar 7 completes without taking its low.
    """
    signals = _drive(
        Mme9FailedTurnStrategy(side=Side.LONG, period=3),
        _candles(
            [
                *_RISING,
                _FAILURE,
                _RECOVERY,
                ("103", "105.0", "102.0"),  # bends down: cancel, and the new failure bar
                ("106", "106.5", "102.5"),  # recovers above 102.0: arms again
            ]
        ),
    )

    assert _kinds(signals)[5:] == [["ENTRY"], ["CANCEL"], ["ENTRY"]]
    assert signals[6][0].client_id == signals[5][0].client_id
    # Measured against the new failure bar, not the old one.
    assert _levels(signals[7][0]) == ("106.5", "102.00")


def test_the_sell_side_is_the_mirror() -> None:
    """A falling MME3, one bar lifting it, and the next one pushing it back down."""
    signals = _drive(
        Mme9FailedTurnStrategy(side=Side.SHORT, period=3),
        _candles(
            [
                ("106", "106.5", "105.5"),
                ("105", "105.5", "104.5"),
                ("104", "104.5", "103.5"),
                ("100", "101.0", "99.5"),  # the average is at 102.5 and falling
                ("104", "105.0", "102.0"),  # failure: it lifts. High of 105.0
                ("101", "104.5", "100.2"),  # recovery: back down, and under 105.0
            ]
        ),
    )

    assert _kinds(signals) == [[], [], [], [], [], ["ENTRY"]]
    entry = signals[5][0]
    assert entry.side is Side.SHORT
    # Sells the recovery bar's low, protected above the **failure** bar's high — 105.00, not the
    # 104.5 of the bar it entered on.
    assert _levels(entry) == ("100.2", "105.00")


def test_a_sell_recovery_that_takes_the_failure_bars_high_is_refused() -> None:
    """The mirror of rule 4 — and of its boundary, which is the half a buy-only suite leaves alive.

    A sell recovery touching the failure bar's high exactly is the double top on one level, and it
    arms for the same reason its mirror does.
    """
    falling: list[tuple[str, str, str]] = [
        ("106", "106.5", "105.5"),
        ("105", "105.5", "104.5"),
        ("104", "104.5", "103.5"),
        ("100", "101.0", "99.5"),
        ("104", "105.0", "102.0"),
    ]

    violated = _drive(
        Mme9FailedTurnStrategy(side=Side.SHORT, period=3),
        # Its high clears 105.0: a 9.1 buy, not a 9.4 sell.
        _candles([*falling, ("101", "105.5", "100.2")]),
    )
    assert _kinds(violated) == [[], [], [], [], [], []]

    touched = _drive(
        Mme9FailedTurnStrategy(side=Side.SHORT, period=3),
        _candles([*falling, ("101", "105.0", "100.2")]),
    )
    assert _kinds(touched) == [[], [], [], [], [], ["ENTRY"]]
    assert _levels(touched[5][0]) == ("100.2", "105.00")


def test_nothing_arms_beside_an_open_trade() -> None:
    held = Position(
        symbol=AAPL.symbol,
        side=Side.LONG,
        volume=Decimal(1),
        entry_price=Decimal("105.8"),
        entry_time=_candles(_RISING)[0].time,
    )
    signals = _drive(
        Mme9FailedTurnStrategy(side=Side.LONG, period=3),
        _candles([*_RISING, _FAILURE, _RECOVERY]),
        position_on=frozenset({5}),
        held=held,
    )

    assert _kinds(signals) == [[], [], [], [], [], []]


def test_breakeven_is_off_by_default_and_arms_when_it_is_asked_for() -> None:
    held = Position(
        symbol=AAPL.symbol,
        side=Side.LONG,
        volume=Decimal(1),
        entry_price=Decimal("105.8"),
        entry_time=_candles(_RISING)[0].time,
        stop_loss=Decimal("101.00"),
        initial_stop_loss=Decimal("101.00"),
    )
    # Risk is 4.80, so twice it is 115.40 — which bar 6 reaches.
    candles = _candles([*_RISING, _FAILURE, _RECOVERY, ("115", "116.0", "105.0")])

    off = _drive(
        Mme9FailedTurnStrategy(side=Side.LONG, period=3),
        candles,
        position_on=frozenset({6}),
        held=held,
    )
    assert off[6] == []

    on = _drive(
        Mme9FailedTurnStrategy(side=Side.LONG, period=3, breakeven_at_r=Decimal(2)),
        candles,
        position_on=frozenset({6}),
        held=held,
    )
    (moved,) = on[6]
    assert moved.kind is SignalKind.MODIFY_STOP
    assert str(moved.stop_loss) == "105.8"


def test_the_buffer_pushes_the_stop_past_the_failure_bar_on_both_sides() -> None:
    """Three ticks of AAPL is three cents, and it moves the level away from the trade."""
    buy = _drive(
        Mme9FailedTurnStrategy(side=Side.LONG, period=3, stop_buffer_ticks=3),
        _candles([*_RISING, _FAILURE, _RECOVERY]),
    )
    assert _levels(buy[5][0]) == ("105.8", "100.97")

    sell = _drive(
        Mme9FailedTurnStrategy(side=Side.SHORT, period=3, stop_buffer_ticks=3),
        _candles(
            [
                ("106", "106.5", "105.5"),
                ("105", "105.5", "104.5"),
                ("104", "104.5", "103.5"),
                ("100", "101.0", "99.5"),
                ("104", "105.0", "102.0"),
                ("101", "104.5", "100.2"),
            ]
        ),
    )
    assert _levels(sell[5][0]) == ("100.2", "105.03")


def test_the_stop_order_fills_on_a_later_bar_at_the_trigger() -> None:
    """Through the real loop, where the fill belongs to a bar that had not happened yet.

    Bar 6 closes *under* the trigger and reaches over it intrabar, which is the fill this setup is
    supposed to get. A bar that opened above 105.8 would fill at its open instead — honest, but it
    would prove the broker's gap handling rather than this setup's trigger.
    """
    candles = _candles([*_RISING, _FAILURE, _RECOVERY, ("105.5", "106.2", "105.0")])
    result: RunResult = run(
        candles=candles,
        timeframe=HOUR,
        instrument=AAPL,
        strategy=Mme9FailedTurnStrategy(side=Side.LONG, period=3),
        broker=BacktestBroker(instrument=AAPL, initial_capital=Decimal(100_000)),
        risk=PercentRiskManager(percent=Decimal("1")),
    )

    entries = [fill for fill in result.fills if fill.order.intent is SignalKind.ENTRY]
    assert [str(fill.price) for fill in entries] == ["105.8"]
    assert entries[0].time > candles[5].time
    assert str(entries[0].order.stop_loss) == "101.00"

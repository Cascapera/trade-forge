"""The published 9.2 and 9.3: the leg's best close is the anchor, the correction is the trade.

One machine with a threshold — `corrections=1` is 9.2 and `corrections=2` is 9.3 — so most of this
file drives both over the same bars and reads the difference. The two rules that are easy to write
wrongly and impossible to see in a P&L are pinned hardest:

* **the anchor is the highest *close*, not the highest high**, so a bar that prints above the leg
  and closes under it is a correction rather than a new leg;
* **the stop is the low of the whole correction, not of the bar that triggers**, which coincide
  whenever the deepest bar is also the last one — the only fixture where a test would notice is
  one where they differ.

The bars are hand-built so the MME3 (short, to keep the warmup readable) rises where the comment
says it does. Golden numbers come from feeding these exact bars to the engine.
"""

from decimal import Decimal, localcontext

import pytest

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
from tradeforge_engine.swing import Mme9PullbackStrategy
from tradeforge_engine.testing import AAPL, HOUR, ImmediateFillBroker, bar

_ACCOUNT = ImmediateFillBroker(instrument=AAPL).account()

# A rising MME3 whose best close is bar 3 — the anchor every scenario below corrects against.
_LEG: list[tuple[str, str, str]] = [
    ("100", "100.5", "99.5"),
    ("101", "101.5", "100.5"),
    ("102", "102.5", "101.5"),
    ("105", "105.5", "104.5"),
]

# Two bars under that anchor. ⚠️ The **first** is the deeper one (102.0 against 103.5), which is
# what makes "the low of the correction" and "the low of the trigger bar" different numbers.
_CORRECTION: list[tuple[str, str, str]] = [
    ("104", "104.6", "102.0"),
    ("104.5", "105.0", "103.5"),
]


def _candles(rows: list[tuple[str, str, str]]) -> list[Candle]:
    """Bars from `(close, high, low)`. The open is the close, so a stop order on the next bar is
    never refused for gapping past its trigger."""
    return [
        bar(index, open_=close, close=close, high=high, low=low)
        for index, (close, high, low) in enumerate(rows)
    ]


def _drive(
    strategy: Mme9PullbackStrategy,
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


def test_one_close_under_the_anchor_arms_the_92() -> None:
    signals = _drive(
        Mme9PullbackStrategy(side=Side.LONG, corrections=1, period=3),
        _candles(_LEG + _CORRECTION),
    )

    # Bar 3 is the anchor and arms nothing: it is the leg, not the correction.
    assert _kinds(signals)[:4] == [[], [], [], []]
    (entry,) = signals[4]
    assert entry.kind is SignalKind.ENTRY
    assert _levels(entry) == ("104.6", "102.00")
    assert entry.reason == "entry.mme9pull"
    # The anchor travels with the entry, because it is the whole qualification.
    assert entry.context == {"average": Decimal("103.5"), "reference_close": Decimal("105")}


def test_the_93_waits_for_the_second_close_and_keeps_the_first_bars_low() -> None:
    """⚠️ The separator for "the stop is the correction, not the trigger bar".

    The 9.3 triggers on bar 5, whose own low is 103.5 — and its stop is **102.00**, the low of bar
    4. A version that protected the trigger bar would risk a third of what this one risks, and
    every backtest it produced would look better for it.
    """
    signals = _drive(
        Mme9PullbackStrategy(side=Side.LONG, corrections=2, period=3),
        _candles(_LEG + _CORRECTION),
    )

    assert _kinds(signals) == [[], [], [], [], [], ["ENTRY"]]
    assert _levels(signals[5][0]) == ("105.0", "102.00")


def test_the_trigger_follows_the_later_bars_and_the_stop_does_not() -> None:
    """*"Podendo ser mudado para as máximas seguintes, desde que a MME9 siga ascendente"*.

    The order chases forward one bar at a time while the correction goes on. What does not move is
    the protective level: bar 6 is shallower than bar 4, and the risk of the trade stays measured
    from the deepest bar of the pullback.
    """
    signals = _drive(
        Mme9PullbackStrategy(side=Side.LONG, corrections=1, period=3),
        _candles(_LEG + _CORRECTION + [("104.2", "104.8", "103.8")]),
    )

    assert _kinds(signals)[4:] == [["ENTRY"], ["CANCEL", "ENTRY"], ["CANCEL", "ENTRY"]]
    assert [_levels(bars[-1]) for bars in signals[4:]] == [
        ("104.6", "102.00"),
        ("105.0", "102.00"),
        ("104.8", "102.00"),
    ]
    # And the cancel names the order it replaces, rather than being a cancel of nothing.
    assert signals[5][0].client_id == signals[4][0].client_id


def test_a_close_back_above_the_anchor_is_the_leg_resuming() -> None:
    """The count restarts from a new anchor, and what was resting is withdrawn.

    Keeping the count across a better close is the mistake this pins: two bars under an anchor
    with a fresh leg high between them are not a correction, and calling them one is how a 9.3
    quietly becomes a 9.2 in a trend that keeps making highs.
    """
    signals = _drive(
        Mme9PullbackStrategy(side=Side.LONG, corrections=1, period=3),
        _candles(
            [
                *_LEG,
                ("104", "104.6", "102.0"),  # correction under the anchor of 105
                ("106", "106.5", "104.0"),  # a better close: this bar is the new anchor
                ("105.5", "106.2", "104.8"),  # one under *that* one, so the 9.2 arms again
            ]
        ),
    )

    assert _kinds(signals)[4:] == [["ENTRY"], ["CANCEL"], ["ENTRY"]]
    # The second entry is measured against the new anchor: its own bar's low, not 102.00.
    assert _levels(signals[6][0]) == ("106.2", "104.80")
    assert signals[6][0].context == {
        "average": Decimal("105.125"),
        "reference_close": Decimal("106"),
    }


def test_the_average_bending_down_forgets_the_leg_and_not_only_the_order() -> None:
    """The cancel is the visible half; the anchor going with it is the half that bites later.

    ⚠️ A test that stops at the withdrawal cannot tell "forgot everything" from "cancelled and
    kept the state". The bars after the bend are what separate them: the average turns back up at
    a completely different level, and bar 6 has to be read as a **new anchor** rather than as a
    correction against the 105 of a leg that is over. Kept state would cancel an order that was
    already cancelled — an orphan `client_id` reaching a live broker — and then arm a trade with
    a stop inherited from another movement.
    """
    signals = _drive(
        Mme9PullbackStrategy(side=Side.LONG, corrections=1, period=3),
        _candles(
            [
                *_LEG,
                ("104", "104.6", "102.0"),  # arms against the anchor of 105
                ("95", "104.0", "94.0"),  # the line bends down: withdrawn, and forgotten
                ("100", "100.5", "98.0"),  # rising again, and this bar is the new anchor
                ("99.8", "100.3", "98.5"),  # one close under *it*, so the 9.2 arms again
            ]
        ),
    )

    assert _kinds(signals)[4:] == [["ENTRY"], ["CANCEL"], [], ["ENTRY"]]
    assert signals[5][0].reason == "cancel.mme9pull"
    # Measured against the new anchor: bar 7's own low, and nothing of the dead correction.
    assert _levels(signals[7][0]) == ("100.3", "98.50")
    context = signals[7][0].context
    assert context is not None
    assert context["reference_close"] == Decimal("100")


def test_the_anchor_is_the_highest_close_and_not_the_highest_high() -> None:
    """A bar that prints above the leg and closes under it corrects; it does not become the anchor.

    ⚠️ Anchoring on the high is the plausible wrong reading, and it is invisible in the arming bar
    of an ordinary pullback — the two only come apart when a corrective bar carries the leg's
    biggest wick, which is exactly what bar 4 does here (high 107.0 over the anchor's 105.5).
    """
    signals = _drive(
        Mme9PullbackStrategy(side=Side.LONG, corrections=1, period=3),
        _candles([*_LEG, ("104", "107.0", "102.0"), ("104.3", "104.9", "103.9")]),
    )

    # Under the rival reading bar 4 would be a new anchor and arm nothing at all.
    assert _kinds(signals)[4:] == [["ENTRY"], ["CANCEL", "ENTRY"]]
    assert _levels(signals[4][0]) == ("107.0", "102.00")


def test_a_close_exactly_on_the_anchor_neither_corrects_nor_advances() -> None:
    """The knife-edge bar, and it separates three readings at once.

    Bar 5 closes at 105, the anchor's own close. Counted as a correction it would complete the 9.3
    there; counted as an advance it would reset, and bar 6 would be a first correction that arms
    nothing. Leaving the state alone is what makes bar 6 the second bar of the same pullback.
    """
    signals = _drive(
        Mme9PullbackStrategy(side=Side.LONG, corrections=2, period=3),
        _candles(
            [
                *_LEG,
                ("104", "104.6", "102.0"),
                # ⚠️ Deeper than bar 4, deliberately: the bar that does nothing does not extend
                # the correction's low either, so 101.0 never reaches the stop. That is the rule
                # applied literally, and it is a simplification — the low is inside the pullback
                # by any chart's reading. Noted in `specs/backlog.md` as a question for the author.
                ("105", "105.2", "101.0"),
                ("104.5", "105.1", "103.9"),
            ]
        ),
    )

    assert _kinds(signals)[4:] == [[], [], ["ENTRY"]]
    # The stop reaches back to bar 4, through the bar that did nothing and past its deeper low.
    assert _levels(signals[6][0]) == ("105.1", "102.00")


def test_the_sell_side_is_the_mirror() -> None:
    """A falling MME3, the anchor on the lowest close, and the correction bought back upwards."""
    signals = _drive(
        Mme9PullbackStrategy(side=Side.SHORT, corrections=2, period=3),
        _candles(
            [
                ("105", "105.5", "104.5"),
                ("104", "104.5", "103.5"),
                ("103", "103.5", "102.5"),
                ("100", "100.5", "99.5"),  # the anchor: the leg's lowest close
                ("101", "103.0", "100.4"),  # correction 1, and the tallest bar of it
                ("100.5", "101.5", "100.0"),  # correction 2: the trigger
            ]
        ),
    )

    assert _kinds(signals) == [[], [], [], [], [], ["ENTRY"]]
    entry = signals[5][0]
    assert entry.side is Side.SHORT
    # Sells at the low of the trigger bar, protected above the **correction's** high — 103.0 from
    # bar 4, not the 101.5 of the bar it entered on.
    assert _levels(entry) == ("100.0", "103.00")


def test_the_sell_anchor_is_a_close_and_survives_both_a_deeper_wick_and_an_equal_close() -> None:
    """The two rules the buy side has tests for, mirrored — and one fixture kills three readings.

    Bar 4 corrects (it closes above the anchor) while printing a **lower low** than the anchor
    itself, which is the exhaustion wick at the end of a down leg. An implementation anchored on
    the lowest *low* would call that bar a new leg and this pullback would not exist.

    Bar 5 then closes exactly at the anchor's close. Counted as a correction it completes the 9.3
    a bar early — more trades, every one of them plausible; counted as a new anchor it resets, and
    the 9.3 never fires here at all.
    """
    signals = _drive(
        Mme9PullbackStrategy(side=Side.SHORT, corrections=2, period=3),
        _candles(
            [
                ("105", "105.5", "104.5"),
                ("104", "104.5", "103.5"),
                ("103", "103.5", "102.5"),
                ("100", "100.5", "99.5"),  # anchor: lowest close, low of 99.5
                ("101", "103.0", "99.0"),  # correction 1, and its low undercuts the anchor's
                ("100", "100.8", "99.2"),  # closes exactly on the anchor: neither
                ("100.5", "101.2", "100.1"),  # correction 2: the trigger
            ]
        ),
    )

    assert _kinds(signals) == [[], [], [], [], [], [], ["ENTRY"]]
    assert _levels(signals[6][0]) == ("100.1", "103.00")


def test_nothing_arms_beside_an_open_trade_and_the_correction_is_spent_after_a_fill() -> None:
    """One trade per correction: the bars after the fill do not re-arm the pullback that gave it."""
    held = Position(
        symbol=AAPL.symbol,
        side=Side.LONG,
        volume=Decimal(1),
        entry_price=Decimal("104.6"),
        entry_time=_candles(_LEG)[0].time,
    )
    signals = _drive(
        Mme9PullbackStrategy(side=Side.LONG, corrections=1, period=3),
        _candles(_LEG + _CORRECTION + [("104.2", "104.8", "103.8")]),
        position_on=frozenset({5}),
        held=held,
    )

    assert _kinds(signals)[4:] == [["ENTRY"], [], []]


def test_the_stop_order_fills_on_a_later_bar_at_the_trigger() -> None:
    """Through the real loop, where a breakout is a breakout and the fill belongs to a later bar."""
    candles = _candles(_LEG + _CORRECTION)
    result: RunResult = run(
        candles=candles,
        timeframe=HOUR,
        instrument=AAPL,
        strategy=Mme9PullbackStrategy(side=Side.LONG, corrections=1, period=3),
        broker=BacktestBroker(instrument=AAPL, initial_capital=Decimal(100_000)),
        risk=PercentRiskManager(percent=Decimal("1")),
    )

    entries = [fill for fill in result.fills if fill.order.intent is SignalKind.ENTRY]
    assert [str(fill.price) for fill in entries] == ["104.6"]
    assert entries[0].time > candles[4].time
    # Sized against the correction's low, which is the level the order actually carried.
    assert str(entries[0].order.stop_loss) == "102.00"


def test_the_buffer_pushes_the_stop_past_the_correction_on_both_sides() -> None:
    """`stop_buffer_ticks` is a magnitude in ticks, and it moves the level *away* from the trade.

    Routing is proved elsewhere; what is proved here is the arithmetic and its sign, which is the
    part a mirror gets wrong. Three ticks of AAPL is three cents: under 102.00 for a buy, over
    103.00 for a sell.
    """
    buy = _drive(
        Mme9PullbackStrategy(side=Side.LONG, corrections=1, period=3, stop_buffer_ticks=3),
        _candles([*_LEG, _CORRECTION[0]]),
    )
    assert _levels(buy[4][0]) == ("104.6", "101.97")

    sell = _drive(
        Mme9PullbackStrategy(side=Side.SHORT, corrections=1, period=3, stop_buffer_ticks=3),
        _candles(
            [
                ("105", "105.5", "104.5"),
                ("104", "104.5", "103.5"),
                ("103", "103.5", "102.5"),
                ("100", "100.5", "99.5"),
                ("101", "103.0", "100.4"),
            ]
        ),
    )
    assert _levels(sell[4][0]) == ("100.4", "103.03")


def test_breakeven_is_off_by_default_and_arms_when_it_is_asked_for() -> None:
    """The one rule here that is a number, and the published setup does not carry it."""
    held = Position(
        symbol=AAPL.symbol,
        side=Side.LONG,
        volume=Decimal(1),
        entry_price=Decimal("104.6"),
        entry_time=_candles(_LEG)[0].time,
        stop_loss=Decimal("102.00"),
        initial_stop_loss=Decimal("102.00"),
    )
    # Risk is 2.60, so twice it is 109.80 — which bar 5 reaches.
    candles = _candles([*_LEG, _CORRECTION[0], ("110", "110.5", "104.8")])

    off = _drive(
        Mme9PullbackStrategy(side=Side.LONG, corrections=1, period=3),
        candles,
        position_on=frozenset({5}),
        held=held,
    )
    assert off[5] == []

    on = _drive(
        Mme9PullbackStrategy(side=Side.LONG, corrections=1, period=3, breakeven_at_r=Decimal(2)),
        candles,
        position_on=frozenset({5}),
        held=held,
    )
    (moved,) = on[5]
    assert moved.kind is SignalKind.MODIFY_STOP
    assert str(moved.stop_loss) == "104.6"


def test_a_correction_is_at_least_one_closed_bar() -> None:
    with pytest.raises(ValueError, match="at least one closed bar"):
        Mme9PullbackStrategy(side=Side.LONG, corrections=0)

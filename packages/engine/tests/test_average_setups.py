"""The clock a moving average keeps for his four bar patterns.

Driven below the strategy on purpose: the host (`swing.py`) owns the turn, and what is under test
here is everything the turn does *not* own — the touch, the window of one bar after it, the second
bar, the order's two bars and the pattern's own annulments. Every sell-side scenario is the buy
side **reflected**, bars and average alike, for the reason `test_bar_setups.py` gives: a hand-typed
sell fixture is a second chance to make the same reading error.

The numbers were read off the watch before they were written here.
"""

from decimal import Decimal

import pytest

from tradeforge_engine.average_setups import (
    AverageEntryPoint,
    PatternOrder,
    PatternWatch,
)
from tradeforge_engine.bar_setups import GiftStop
from tradeforge_engine.domain import Candle, Side
from tradeforge_engine.errors import EngineError
from tradeforge_engine.testing import bar

PENNY = Decimal("0.01")
MIRROR = Decimal(200)


def flip(candle: Candle) -> Candle:
    """The bar reflected around 200 — highs become lows, up becomes down."""
    return Candle(
        time=candle.time,
        open=MIRROR - candle.open,
        high=MIRROR - candle.low,
        low=MIRROR - candle.high,
        close=MIRROR - candle.close,
        tick_volume=candle.tick_volume,
    )


def watch(entry_point: AverageEntryPoint, **kwargs: object) -> PatternWatch:
    return PatternWatch(entry_point=entry_point, side=Side.LONG, **kwargs)  # type: ignore[arg-type]


def feed(
    live: PatternWatch, bars: list[tuple[Candle, str]], *, mirrored: bool = False
) -> list[PatternOrder | None]:
    """Feed `(candle, average)` pairs, reflecting both when `mirrored`."""
    out: list[PatternOrder | None] = []
    for candle, average in bars:
        if mirrored:
            out.append(live.observe(flip(candle), MIRROR - Decimal(average), tick=PENNY))
        else:
            out.append(live.observe(candle, Decimal(average), tick=PENNY))
    return out


def both_sides(
    entry_point: AverageEntryPoint, bars: list[tuple[Candle, str]], **kwargs: object
) -> tuple[list[PatternOrder | None], list[PatternOrder | None]]:
    """The same scenario on the buy side and on its reflection."""
    long = feed(watch(entry_point, **kwargs), bars)
    short = feed(
        PatternWatch(entry_point=entry_point, side=Side.SHORT, **kwargs),  # type: ignore[arg-type]
        bars,
        mirrored=True,
    )
    return long, short


# The average sits at 100 throughout unless a scenario says otherwise. A hammer touching it:
# tail from the open at 100.40 down to 99.20 is 1.20 of a 1.70 bar, closing up.
HAMMER_ON_THE_AVERAGE = bar(0, open_="100.4", close="100.7", high="100.9", low="99.2")
FORCE_ON_THE_AVERAGE = bar(0, open_="99.5", close="103", high="103.2", low="99.3")
GIFT = bar(1, open_="102.9", close="102.8", high="103.1", low="102.5")
IGNORED = bar(1, open_="103", close="101", high="103.1", low="100.5")


def quiet(index: int) -> Candle:
    """Above the average, and **not** a hammer: a tail of 0.05 on a 0.35 bar. The first version
    of this bar had a 0.20 tail — fifty-seven percent — and was a hammer, so a scenario meant to
    show the window closing armed on the quiet bar instead. The fixture that does not separate."""
    return bar(index, open_="100.55", close="100.8", high="100.85", low="100.5")


def touch_without_pattern(index: int) -> Candle:
    """Reaches the average at 100 and is neither a hammer nor a bar of force: a small red bar
    with a modest tail."""
    return bar(index, open_="100.6", close="100.4", high="100.8", low="99.9")


# --------------------------------------------------------------------------- #
# The hammer on the average                                                    #
# --------------------------------------------------------------------------- #


def test_a_hammer_touching_the_average_arms_a_stop_past_its_high() -> None:
    """The touching bar is itself the hammer: order at 100.91, stop 98.86 — twenty percent of
    the bar under 99.20. Nothing about the average is in either number."""
    long, short = both_sides(AverageEntryPoint.MARTELO, [(HAMMER_ON_THE_AVERAGE, "100")])
    assert long[0] == PatternOrder(
        side=Side.LONG, stop_price=Decimal("100.91"), stop_loss=Decimal("98.86")
    )
    assert short[0] == PatternOrder(
        side=Side.SHORT, stop_price=Decimal("99.09"), stop_loss=Decimal("101.14")
    )


def test_the_bar_after_the_touch_may_be_the_hammer_and_the_one_after_that_may_not() -> None:
    """*"A barra que encosta ou a seguinte."* Bar 0 touches without a pattern; a hammer on bar 1
    arms. With one more bar between, the hammer on bar 2 is outside the window and arms nothing
    — and it never touched the average itself (low 100.20)."""
    hammer_above = bar(1, open_="101.4", close="101.7", high="101.9", low="100.2")
    in_time, in_time_short = both_sides(
        AverageEntryPoint.MARTELO, [(touch_without_pattern(0), "100"), (hammer_above, "100")]
    )
    assert in_time[1] is not None
    assert in_time[1].stop_price == Decimal("101.91")
    assert in_time_short[1] is not None

    late_hammer = bar(2, open_="101.4", close="101.7", high="101.9", low="100.2")
    late, late_short = both_sides(
        AverageEntryPoint.MARTELO,
        [(touch_without_pattern(0), "100"), (quiet(1), "100"), (late_hammer, "100")],
    )
    assert late == [None, None, None]
    assert late_short == [None, None, None]


def test_a_bar_resting_exactly_on_the_average_has_touched_it() -> None:
    """Piercing counts and so does the low stopping precisely on the line; one tick above it
    has not touched, and with no touch before it nothing arms."""
    on_it = bar(0, open_="100.6", close="100.8", high="100.9", low="100")
    long, short = both_sides(AverageEntryPoint.MARTELO, [(on_it, "100")])
    assert long[0] is not None
    assert short[0] is not None, "the sell side's touch is its own comparison, and its own line"
    above_it = bar(0, open_="100.6", close="100.8", high="100.9", low="100.01")
    long, short = both_sides(AverageEntryPoint.MARTELO, [(above_it, "100")])
    assert long[0] is None
    assert short[0] is None


def test_there_is_no_ceiling_on_the_average() -> None:
    """*"Sem teto."* A hammer whose high is thirty points above the average — after a touch on
    the bar before — arms all the same; on a region this would be far past any ceiling."""
    far = bar(1, open_="128", close="129", high="130", low="125")
    long, short = both_sides(
        AverageEntryPoint.MARTELO, [(touch_without_pattern(0), "100"), (far, "100")]
    )
    assert long[1] is not None
    assert long[1].stop_price == Decimal("130.01")
    assert short[1] is not None


def test_the_order_lives_two_bars_and_the_turn_is_not_spent() -> None:
    """The order armed on 0 rests through 1 and is gone after 2 — and nothing is spent: a fresh
    touch and hammer on 3 arms again. The region would have retired itself; his fourth answer
    is that only a close across the average ends anything, and the host reads that."""
    long, short = both_sides(
        AverageEntryPoint.MARTELO,
        [
            (HAMMER_ON_THE_AVERAGE, "100"),
            (quiet(1), "100"),
            (quiet(2), "100"),
            (bar(3, open_="100.4", close="100.7", high="100.9", low="99.2"), "100"),
        ],
    )
    assert [order is not None for order in long] == [True, True, False, True]
    assert [order is not None for order in short] == [True, True, False, True]


def test_the_order_s_life_is_the_dial() -> None:
    three = feed(
        watch(AverageEntryPoint.MARTELO, bars_to_fill=3),
        [(HAMMER_ON_THE_AVERAGE, "100"), (quiet(1), "100"), (quiet(2), "100"), (quiet(3), "100")],
    )
    assert [order is not None for order in three] == [True, True, True, False]


def test_losing_the_hammer_s_low_takes_the_order_back_before_the_clock() -> None:
    """The hammer's own rule, unchanged from the region: reaching its low (99.20) on the bar
    after arming ends the order there. The same bar one tick short of it leaves the order."""
    reaches = bar(1, open_="100.6", close="100.5", high="100.7", low="99.2")
    long, short = both_sides(
        AverageEntryPoint.MARTELO, [(HAMMER_ON_THE_AVERAGE, "100"), (reaches, "100")]
    )
    assert long[1] is None
    assert short[1] is None

    short_of = bar(1, open_="100.6", close="100.5", high="100.7", low="99.21")
    long, short = both_sides(
        AverageEntryPoint.MARTELO, [(HAMMER_ON_THE_AVERAGE, "100"), (short_of, "100")]
    )
    assert long[1] is not None
    assert short[1] is not None


def test_a_second_hammer_while_one_rests_changes_nothing() -> None:
    """One order at a time. The hammer on bar 1, itself touching the average, does not re-price
    the order the hammer on bar 0 placed — the levels are the first hammer's until it is gone."""
    second = bar(1, open_="100.5", close="100.8", high="101.2", low="99.5")
    long = feed(watch(AverageEntryPoint.MARTELO), [(HAMMER_ON_THE_AVERAGE, "100"), (second, "100")])
    assert long[1] == long[0]


# --------------------------------------------------------------------------- #
# The hammer and the bar of force                                              #
# --------------------------------------------------------------------------- #


FORCE_AFTER_HAMMER = bar(1, open_="100.8", close="103", high="103.2", low="100.6")


def test_the_force_variation_rests_a_limit_between_the_two_highs() -> None:
    """Hammer high 100.90, force bar high 103.20: the limit at 101.59, thirty percent of the way,
    with the stop still the hammer's 98.86. Nothing on the hammer's own bar."""
    long, short = both_sides(
        AverageEntryPoint.MARTELO_FORCA,
        [(HAMMER_ON_THE_AVERAGE, "100"), (FORCE_AFTER_HAMMER, "100")],
    )
    assert long[0] is None
    assert long[1] == PatternOrder(
        side=Side.LONG, limit_price=Decimal("101.59"), stop_loss=Decimal("98.86")
    )
    assert short[1] == PatternOrder(
        side=Side.SHORT, limit_price=Decimal("98.41"), stop_loss=Decimal("101.14")
    )


def test_a_bar_that_is_not_a_bar_of_force_clears_the_hammer_without_spending_anything() -> None:
    """Bar 1 after the hammer is a quiet bar: the wait is over and nothing rests. And the turn
    stands — bar 1 itself touched the average and is read as a fresh first bar, so the hammer on
    bar 2 waits for its own bar of force on 3 and arms."""
    long, short = both_sides(
        AverageEntryPoint.MARTELO_FORCA,
        [
            (HAMMER_ON_THE_AVERAGE, "100"),
            (touch_without_pattern(1), "100"),
            (bar(2, open_="100.4", close="100.7", high="100.9", low="99.2"), "100"),
            (bar(3, open_="100.8", close="103", high="103.2", low="100.6"), "100"),
        ],
    )
    assert [order is not None for order in long] == [False, False, False, True]
    assert [order is not None for order in short] == [False, False, False, True]


def test_the_failed_second_bar_is_itself_read_as_a_first_bar() -> None:
    """The bar after the hammer that is not a bar of force but **is** a hammer becomes the
    hammer: its bar of force on the next bar arms, one bar earlier than a machine that only
    cleared the wait would manage."""
    second_hammer = bar(1, open_="100.5", close="100.8", high="101", low="99.4")
    force = bar(2, open_="100.9", close="103", high="103.3", low="100.7")
    long = feed(
        watch(AverageEntryPoint.MARTELO_FORCA),
        [(HAMMER_ON_THE_AVERAGE, "100"), (second_hammer, "100"), (force, "100")],
    )
    assert long[2] is not None
    assert long[2].limit_price == Decimal("101.69")  # 101 + 0.30 x 2.30


def test_the_force_variation_cancels_on_a_close_beyond_the_force_bar_on_the_next_bar_only() -> None:
    """His rule: a bar *closing* beyond the force bar's high on the bar after it withdraws the
    limit — the pullback is not coming. Wicking past and closing back under leaves it. And only
    on that bar: at three bars of life, a close beyond on the second bar after leaves it too."""
    closes_beyond = bar(2, open_="103.1", close="103.5", high="103.6", low="103")
    wicks_only = bar(2, open_="103.1", close="103.1", high="103.6", low="103")
    armed = [(HAMMER_ON_THE_AVERAGE, "100"), (FORCE_AFTER_HAMMER, "100")]

    long, short = both_sides(AverageEntryPoint.MARTELO_FORCA, [*armed, (closes_beyond, "100")])
    assert long[2] is None
    assert short[2] is None

    long, short = both_sides(AverageEntryPoint.MARTELO_FORCA, [*armed, (wicks_only, "100")])
    assert long[2] is not None
    assert short[2] is not None

    # ⚠️ *"Fechar acima"* is strict: a bar closing exactly **on** the force bar's high (103.20)
    # has not closed beyond it, and the limit stays. Neither fixture above lands on the number,
    # so `>=` for `>` survived both of them — the guardian's finding, on both sides.
    closes_on_it = bar(2, open_="103.1", close="103.2", high="103.6", low="103")
    long, short = both_sides(AverageEntryPoint.MARTELO_FORCA, [*armed, (closes_on_it, "100")])
    assert long[2] is not None
    assert short[2] is not None

    later = feed(
        watch(AverageEntryPoint.MARTELO_FORCA, bars_to_fill=3),
        [
            *armed,
            (wicks_only, "100"),
            (bar(3, open_="103.1", close="103.5", high="103.6", low="103"), "100"),
        ],
    )
    assert later[3] is not None


# --------------------------------------------------------------------------- #
# The gift and the ignored bar                                                 #
# --------------------------------------------------------------------------- #


def test_the_gift_on_the_average_arms_past_the_higher_high_with_the_chosen_stop() -> None:
    """Force bar touching the average, gift after it: order at 103.21, stop 102.38 off the gift
    (a fifth of its 0.60 under 102.50) or 98.52 off the force bar (a fifth of 3.90 under 99.30).
    Nothing on the force bar's own bar."""
    long, short = both_sides(AverageEntryPoint.GIFT, [(FORCE_ON_THE_AVERAGE, "100"), (GIFT, "100")])
    assert long[0] is None
    assert long[1] == PatternOrder(
        side=Side.LONG, stop_price=Decimal("103.21"), stop_loss=Decimal("102.38")
    )
    assert short[1] == PatternOrder(
        side=Side.SHORT, stop_price=Decimal("96.79"), stop_loss=Decimal("97.62")
    )

    off_force, off_force_short = both_sides(
        AverageEntryPoint.GIFT,
        [(FORCE_ON_THE_AVERAGE, "100"), (GIFT, "100")],
        gift_stop=GiftStop.FORCA,
    )
    assert off_force[1] is not None
    assert off_force[1].stop_loss == Decimal("98.52")
    assert off_force_short[1] is not None
    assert off_force_short[1].stop_loss == Decimal("101.48")


def test_the_ignored_bar_on_the_average_arms_with_the_force_bar_s_stop() -> None:
    long, short = both_sides(
        AverageEntryPoint.BARRA_IGNORADA, [(FORCE_ON_THE_AVERAGE, "100"), (IGNORED, "100")]
    )
    assert long[1] == PatternOrder(
        side=Side.LONG, stop_price=Decimal("103.21"), stop_loss=Decimal("98.52")
    )
    assert short[1] == PatternOrder(
        side=Side.SHORT, stop_price=Decimal("96.79"), stop_loss=Decimal("101.48")
    )


def test_each_follower_is_silent_on_the_other_s_bar() -> None:
    """The gift setup handed an ignored bar arms nothing, and the ignored-bar setup handed a gift
    arms nothing either.

    ⚠️ **And bar 2 is silent for a reason that has nothing to do with the follower.** An earlier
    docstring here claimed the ignored bar on 1 was "a bar of force in its own right" and that the
    gift on 2 armed off it: false, and the assertion three lines below always said so. `IGNORED`
    closes **down** (103 to 101), and `is_force_bar` refuses a bar whose body points against the
    side before it measures anything. The lesson's review of this PR caught the contradiction
    between the prose and the assertion it sat on. "Nothing is spent" is proved by
    `test_a_failed_follower_leaves_the_turn_standing` below, which needs a force bar that is
    actually one.
    """
    gift_after = bar(2, open_="101.1", close="101", high="101.3", low="100.7")
    long, short = both_sides(
        AverageEntryPoint.GIFT,
        [(FORCE_ON_THE_AVERAGE, "100"), (IGNORED, "100"), (gift_after, "100")],
    )
    assert long[1] is None
    assert long[2] is None, "the ignored bar closed down; it is no bar of force for a long"
    assert short[2] is None

    ignored_given_gift = feed(
        watch(AverageEntryPoint.BARRA_IGNORADA), [(FORCE_ON_THE_AVERAGE, "100"), (GIFT, "100")]
    )
    assert ignored_given_gift == [None, None]


def test_a_failed_follower_leaves_the_turn_standing() -> None:
    """*"Correto"*, 2026-09-07, asked whether a pattern that fails spends anything: it does not.

    Bar 0 is a bar of force touching the average and bar 1 is neither gift nor ignored bar — a
    plain up bar, too tall for a gift and with too small a body for an ignored one. Nothing arms.
    Then bar 2 touches again with a bar of force and the gift on 3 arms at 103.21: the turn never
    ended, because no bar closed across the average.

    On a region this same failure retires the zone (`ForceFollowActivation`), and a run there
    would produce **no** second setup. Two hosts, opposite answers, both his — which is why this
    is asserted rather than assumed from the region's tests.
    """
    neither = bar(1, open_="103.1", close="103.4", high="104.6", low="103")
    force_again = bar(2, open_="99.5", close="103", high="103.2", low="99.3")
    gift_again = bar(3, open_="102.9", close="102.8", high="103.1", low="102.5")
    long, short = both_sides(
        AverageEntryPoint.GIFT,
        [
            (FORCE_ON_THE_AVERAGE, "100"),
            (neither, "100"),
            (force_again, "100"),
            (gift_again, "100"),
        ],
    )
    assert long[1] is None
    assert long[2] is None
    assert long[3] is not None
    assert long[3].stop_price == Decimal("103.21")
    assert short[3] is not None


def test_losing_the_force_bar_s_low_takes_the_follower_s_order_back() -> None:
    """His rule from the region, carried over: reaching the force bar's low (99.30) while the
    order rests annuls it; one tick short leaves it."""
    reaches = bar(2, open_="102.8", close="102", high="102.9", low="99.3")
    long, short = both_sides(
        AverageEntryPoint.GIFT, [(FORCE_ON_THE_AVERAGE, "100"), (GIFT, "100"), (reaches, "100")]
    )
    assert long[2] is None
    assert short[2] is None
    short_of = bar(2, open_="102.8", close="102", high="102.9", low="99.31")
    long, short = both_sides(
        AverageEntryPoint.GIFT, [(FORCE_ON_THE_AVERAGE, "100"), (GIFT, "100"), (short_of, "100")]
    )
    assert long[2] is not None
    assert short[2] is not None


def test_the_volume_filter_reaches_the_follower_on_the_average() -> None:
    """A gift at 701 against a force bar at 1000 is refused with the filter on and admitted with
    it off — the dial arrives, and on its non-default value."""

    def loud(candle: Candle, volume: int) -> Candle:
        return Candle(
            time=candle.time,
            open=candle.open,
            high=candle.high,
            low=candle.low,
            close=candle.close,
            tick_volume=volume,
        )

    bars = [(loud(FORCE_ON_THE_AVERAGE, 1000), "100"), (loud(GIFT, 701), "100")]
    assert feed(watch(AverageEntryPoint.GIFT, volume_filter=True), bars)[1] is None
    assert feed(watch(AverageEntryPoint.GIFT), bars)[1] is not None
    quiet_enough = [(loud(FORCE_ON_THE_AVERAGE, 1000), "100"), (loud(GIFT, 700), "100")]
    assert feed(watch(AverageEntryPoint.GIFT, volume_filter=True), quiet_enough)[1] is not None


# --------------------------------------------------------------------------- #
# The host's verb, and the dials                                               #
# --------------------------------------------------------------------------- #


def test_reset_forgets_the_touch_the_first_bar_and_the_order() -> None:
    """The turn ended. Three states, each cleared: a resting hammer order; a hammer waiting for
    its bar of force; and the touch itself — after a reset the hammer on the next bar, which does
    not touch, is outside any window and arms nothing."""
    resting = watch(AverageEntryPoint.MARTELO)
    assert feed(resting, [(HAMMER_ON_THE_AVERAGE, "100")])[0] is not None
    resting.reset()
    assert feed(resting, [(quiet(1), "100")])[0] is None

    waiting = watch(AverageEntryPoint.MARTELO_FORCA)
    feed(waiting, [(HAMMER_ON_THE_AVERAGE, "100")])
    waiting.reset()
    assert feed(waiting, [(FORCE_AFTER_HAMMER, "100")])[0] is None

    touched = watch(AverageEntryPoint.MARTELO)
    feed(touched, [(touch_without_pattern(0), "100")])
    touched.reset()
    hammer_above = bar(1, open_="101.4", close="101.7", high="101.9", low="100.2")
    assert feed(touched, [(hammer_above, "100")])[0] is None


def test_the_classic_entry_keeps_no_watch() -> None:
    with pytest.raises(EngineError, match="classic breakout is the host's own entry"):
        PatternWatch(entry_point=AverageEntryPoint.CLASSIC, side=Side.LONG)


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"bars_after_touch": -1}, "is a count"),
        ({"bars_to_fill": 0}, "at least one bar"),
    ],
)
def test_the_watch_refuses_a_dial_that_would_not_be_a_setup(
    kwargs: dict[str, object], message: str
) -> None:
    with pytest.raises(EngineError, match=message):
        PatternWatch(entry_point=AverageEntryPoint.MARTELO, side=Side.LONG, **kwargs)  # type: ignore[arg-type]


def test_the_window_after_the_touch_is_the_dial() -> None:
    """At zero bars after the touch only the touching bar may be the hammer: the hammer on the
    bar after a touch, which arms at the default, arms nothing."""
    hammer_above = bar(1, open_="101.4", close="101.7", high="101.9", low="100.2")
    bars = [(touch_without_pattern(0), "100"), (hammer_above, "100")]
    assert feed(watch(AverageEntryPoint.MARTELO, bars_after_touch=0), bars)[1] is None
    assert feed(watch(AverageEntryPoint.MARTELO), bars)[1] is not None


def test_a_pattern_order_waits_at_a_limit_or_a_stop_and_says_which() -> None:
    with pytest.raises(ValueError, match="not both and not neither"):
        PatternOrder(side=Side.LONG, stop_loss=Decimal(1))
    with pytest.raises(ValueError, match="not both and not neither"):
        PatternOrder(
            side=Side.LONG, stop_loss=Decimal(1), limit_price=Decimal(2), stop_price=Decimal(3)
        )
    assert PatternOrder(side=Side.LONG, stop_loss=Decimal(1), limit_price=Decimal(2)).price == 2
    assert PatternOrder(side=Side.LONG, stop_loss=Decimal(1), stop_price=Decimal(3)).price == 3

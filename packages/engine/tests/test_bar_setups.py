"""The hammer, the force bar, and the levels his two variations put on them.

Every scenario here is mirrored, and the mirror is built by **reflecting the bar around a price**
rather than by writing a second set of numbers. A bug that reads `low` where it should read `high`
then does not merely produce a wrong number — it produces the *other side's* number, which is the
one failure a hand-written sell fixture keeps agreeing with. That has been the blocking finding on
three engine PRs in a row.

The numbers were read off the real triggers before they were written here (`entrada 101 stop 88`,
`entrada 93`), never worked out on paper. The one number that is his rather than ours is the **93**
of `HammerForceTrigger`: hammer high 90, force bar high 100, order at 93.
"""

from decimal import Decimal

import pytest

from tradeforge_engine.bar_setups import (
    DEFAULT_BODY_FRACTION,
    DEFAULT_SHADOW_FRACTION,
    HammerBreakLevels,
    HammerBreakTrigger,
    HammerForceLevels,
    HammerForceTrigger,
    is_force_bar,
    is_hammer,
)
from tradeforge_engine.domain import Candle, Side
from tradeforge_engine.errors import EngineError
from tradeforge_engine.testing import HOUR, START

TICK = Decimal("1")
PENNY = Decimal("0.01")
MIRROR = Decimal("190")
"""The axis the sell-side fixtures are reflected around. Any value works; this one matches the
VWAP suite so the two read alike."""


def candle(index: int, *, open_: str, high: str, low: str, close: str) -> Candle:
    """A bar built from all four prices — this module is entirely about their proportions, so
    none of them may be inferred the way `testing.bar` infers the extremes from the body."""
    return Candle(
        time=START + index * HOUR,
        open=Decimal(open_),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
    )


def flip(bar: Candle) -> Candle:
    """The same bar reflected around `MIRROR`: highs become lows, up becomes down.

    ⚠️ **This is the whole mirror strategy of the file.** A sell fixture typed by hand is a second
    chance to make the same reading error; a reflection cannot be, because it is derived from the
    buy fixture by one rule applied to every field.
    """
    return Candle(
        time=bar.time,
        open=MIRROR - bar.open,
        high=MIRROR - bar.low,
        low=MIRROR - bar.high,
        close=MIRROR - bar.close,
    )


# The author's hammer: a ten-point bar with six points of tail under a small up body.
HAMMER = candle(0, open_="96", high="100", low="90", close="97")

# The force bar of his second variation: high 100 against the hammer's 90.
FORCE_HAMMER = candle(0, open_="86", high="90", low="80", close="87")
FORCE_BAR = candle(1, open_="91", high="100", low="90.5", close="99")


# --------------------------------------------------------------------------- #
# The bar itself                                                               #
# --------------------------------------------------------------------------- #


def test_a_hammer_is_a_tail_over_half_the_bar_under_a_body_that_closed_up() -> None:
    """His definition, in one bar: ten points tall, six of tail, and a body of one point up."""
    assert is_hammer(HAMMER, side=Side.LONG)
    assert is_hammer(flip(HAMMER), side=Side.SHORT)


def test_a_hammer_for_one_side_is_never_a_hammer_for_the_other() -> None:
    """⚠️ **The separating assertion, and the reason the mirror is a reflection.**

    A hammer is not a shape that happens to favour a side — the side is in the definition twice
    over, in which shadow is measured and in which way the body closed. An implementation that
    dropped the side from either test would pass every assertion above and fail here.
    """
    assert not is_hammer(HAMMER, side=Side.SHORT)
    assert not is_hammer(flip(HAMMER), side=Side.LONG)


def test_a_tail_of_exactly_half_the_bar_is_not_a_hammer() -> None:
    """*"a sombra inferior é **maior** do que 50%"* — so the boundary is out, not in.

    Five points of tail on a ten-point bar. Written `>=` this passes, and the difference between
    the two readings is invisible on any bar that is not split exactly down the middle, which is
    to say invisible on almost every bar a random fixture produces.
    """
    halved = candle(0, open_="95", high="100", low="90", close="96")
    assert halved.open - halved.low == (halved.high - halved.low) * DEFAULT_SHADOW_FRACTION
    assert not is_hammer(halved, side=Side.LONG)
    assert not is_hammer(flip(halved), side=Side.SHORT)


def test_a_long_tail_on_both_sides_is_not_a_hammer() -> None:
    """The denominator is the **whole candle**, not the tail plus the body.

    Three points of lower tail is a lot in absolute terms, and on a seven-point bar it is under
    half — the upper shadow spent the budget. An implementation measuring the tail against the
    body instead would call this a hammer and would be describing a different pattern.
    """
    both_ways = candle(0, open_="93", high="97", low="90", close="93.5")
    assert not is_hammer(both_ways, side=Side.LONG)
    assert not is_hammer(flip(both_ways), side=Side.SHORT)


def test_a_tail_with_no_body_is_not_a_hammer() -> None:
    """*"ele deve possuir um corpo mesmo que pequeno de alguns ticks de alta"*.

    Same six-point tail, same ten-point bar, open equal to close. Stricter than the textbook
    definition, and it is his: the body is what says the defending side actually closed in front.
    """
    bodyless = candle(0, open_="96", high="100", low="90", close="96")
    assert not is_hammer(bodyless, side=Side.LONG)
    assert not is_hammer(flip(bodyless), side=Side.SHORT)


def test_a_body_of_a_single_tick_is_enough() -> None:
    """The other side of the line above, so "must have a body" cannot quietly become a size rule.
    A penny of body on a ten-point bar is *"alguns ticks"*, and it counts."""
    barely = candle(0, open_="96", high="100", low="90", close="96.01")
    assert is_hammer(barely, side=Side.LONG)
    assert is_hammer(flip(barely), side=Side.SHORT)


def test_a_tail_pointing_the_wrong_way_is_not_a_hammer() -> None:
    """A bar with an up body and the long shadow on **top** is not a long hammer, however much
    tail it has. Without this, an implementation that never checked which shadow it measured
    would still pass the bodyless and the boundary cases."""
    upside_down = candle(0, open_="91", high="100", low="90", close="92")
    assert not is_hammer(upside_down, side=Side.LONG)


def test_a_force_bar_is_seventy_percent_body_in_the_side_s_favour() -> None:
    assert is_force_bar(FORCE_BAR, side=Side.LONG)
    assert is_force_bar(flip(FORCE_BAR), side=Side.SHORT)
    assert not is_force_bar(FORCE_BAR, side=Side.SHORT)
    assert not is_force_bar(flip(FORCE_BAR), side=Side.LONG)


def test_a_body_of_exactly_seventy_percent_is_a_force_bar() -> None:
    """⚠️ **`≥` here against `>` for the hammer, and this is the test that holds them apart.**

    His words differ — the hammer's shadow is *maior* than half, the force bar *é* seventy percent
    — and written the same way one of the two stops matching a bar he would trade. Seven points of
    body on a ten-point bar: in, not out.
    """
    exact = candle(0, open_="90", high="100", low="90", close="97")
    assert exact.close - exact.open == (exact.high - exact.low) * DEFAULT_BODY_FRACTION
    assert is_force_bar(exact, side=Side.LONG)
    assert is_force_bar(flip(exact), side=Side.SHORT)


def test_a_body_just_under_seventy_percent_is_not_a_force_bar() -> None:
    """The boundary from the other side, so `≥` cannot quietly become "any body at all"."""
    nearly = candle(0, open_="90", high="100", low="90", close="96.9")
    assert not is_force_bar(nearly, side=Side.LONG)
    assert not is_force_bar(flip(nearly), side=Side.SHORT)


def test_a_marubozu_is_a_force_bar() -> None:
    """All body, no shadows — the purest case of what he described, and the case a `>` reading of
    "70%" would keep while a `<` reading of the shadows would throw out."""
    assert is_force_bar(candle(0, open_="90", high="100", low="90", close="100"), side=Side.LONG)


def test_a_hammer_is_not_a_force_bar_and_a_force_bar_is_not_a_hammer() -> None:
    """The two predicates are used on adjacent bars by the same trigger, so a fixture where both
    answer the same way would let one be substituted for the other without a test noticing."""
    assert is_hammer(HAMMER, side=Side.LONG)
    assert not is_force_bar(HAMMER, side=Side.LONG)
    assert is_force_bar(FORCE_BAR, side=Side.LONG)
    assert not is_hammer(FORCE_BAR, side=Side.LONG)


# --------------------------------------------------------------------------- #
# Variation 1 — superação do martelo                                           #
# --------------------------------------------------------------------------- #


def test_the_break_order_waits_one_tick_past_the_hammer_and_stops_a_fifth_below_it() -> None:
    """His arithmetic on the ten-point hammer: in at 101, stop at 88, and 90 annuls.

    ⚠️ **Three distinct numbers**, and the two lower ones are two points apart here only because
    the hammer is ten points tall. The stop is twenty percent of the bar *beyond* the low; the low
    itself cancels the setup without a trade.
    """
    levels = HammerBreakTrigger().levels_for(HAMMER, side=Side.LONG, tick=TICK)
    assert levels == HammerBreakLevels(
        side=Side.LONG,
        stop_price=Decimal(101),
        stop_loss=Decimal(88),
        annul_price=Decimal(90),
    )
    assert levels.risk == Decimal(13)


def test_the_break_order_is_mirrored_for_a_sell() -> None:
    """The same bar reflected: in at 89, stop at 102, and 100 annuls. Reading `high` for `low`
    anywhere in the long path produces exactly these numbers on the wrong side."""
    levels = HammerBreakTrigger().levels_for(flip(HAMMER), side=Side.SHORT, tick=TICK)
    assert levels == HammerBreakLevels(
        side=Side.SHORT,
        stop_price=Decimal(89),
        stop_loss=Decimal(102),
        annul_price=Decimal(100),
    )
    assert levels.risk == Decimal(13)


def test_the_annul_level_and_the_stop_are_never_the_same_number() -> None:
    """⚠️ The trap `FffdLevels` documents, reached from this setup instead.

    On a hammer small enough that twenty percent of it is under a tick, the stop still has to land
    **past** the low, never on it. Collapsing the two would turn every cancelled setup into a
    losing trade in the ledger, because the level that says "this was not the reversal I thought"
    would be the level that says "this trade is over".
    """
    tiny = candle(0, open_="100.03", high="100.05", low="100.00", close="100.04")
    assert is_hammer(tiny, side=Side.LONG)
    levels = HammerBreakTrigger().levels_for(tiny, side=Side.LONG, tick=PENNY)
    assert levels is not None
    assert levels.annul_price == Decimal("100.00")
    assert levels.stop_loss < levels.annul_price


def test_the_break_trigger_is_silent_on_a_bar_that_is_not_a_hammer() -> None:
    """`None`, not an exception and not a guess — the caller's clock decides what to do with a
    bar that says nothing, and most bars say nothing."""
    plain = candle(0, open_="93", high="97", low="90", close="93.5")
    assert HammerBreakTrigger().levels_for(plain, side=Side.LONG, tick=TICK) is None
    assert HammerBreakTrigger().levels_for(flip(plain), side=Side.SHORT, tick=TICK) is None


def test_the_break_levels_land_on_the_instrument_s_grid_the_costly_way() -> None:
    """Both levels off the grid, both rounded **away** from the trade: the entry up, the stop
    down. This engine's rule for every level — a stop no nearer than the rule said, an entry no
    better.

    ⚠️ **The high itself is off the grid here, and that is what makes the entry's rounding
    observable at all.** `high + n x tick` lands on the grid whenever the high does, so a fixture
    built from a well-formed feed cannot tell `ROUND_CEILING` from `ROUND_FLOOR` on the entry —
    both branches produce the same number and the mutant swapping them survives. Nothing
    validates that a `Candle`'s prices are multiples of the instrument's tick (`Candle` checks
    the body and the timezone, and knows no `tick_size`), so a feed that hands us 10.72 on a
    nickel grid is representable, and `to_tick` is the only thing between such a bar and an order
    at a price the venue would reject.
    """
    offgrid = candle(0, open_="10.60", high="10.72", low="10.00", close="10.65")
    levels = HammerBreakTrigger().levels_for(offgrid, side=Side.LONG, tick=Decimal("0.05"))
    assert levels is not None
    # 10.72 + 0.05 = 10.77, up to 10.80; 20% of 0.72 is 0.144, so 9.856, down to 9.85.
    assert levels.stop_price == Decimal("10.80")
    assert levels.stop_loss == Decimal("9.85")

    # ⚠️ The mirror, and it is here rather than in a test of its own because the sell rounding
    # survived every assertion above — the fourth time in this engine that only the buy side of
    # a rule was pinned. 179.28 - 0.05 = 179.23 rounds **down** to 179.20; the stop at
    # 180.00 + 0.144 rounds **up** to 180.15. Both away from the trade, as on the buy side.
    mirrored = HammerBreakTrigger().levels_for(flip(offgrid), side=Side.SHORT, tick=Decimal("0.05"))
    assert mirrored is not None
    assert mirrored.stop_price == Decimal("179.20")
    assert mirrored.stop_loss == Decimal("180.15")


# --------------------------------------------------------------------------- #
# Variation 2 — martelo + barra de força                                       #
# --------------------------------------------------------------------------- #


def test_the_force_entry_rests_thirty_percent_of_the_way_between_the_two_highs() -> None:
    """**His number.** Hammer high 90, force bar high 100, order at 93 — and it is a *limit*,
    below the market, because the force bar closed at 99 and the entry waits for the pullback."""
    levels = HammerForceTrigger().levels_for(FORCE_HAMMER, FORCE_BAR, side=Side.LONG, tick=TICK)
    assert levels == HammerForceLevels(
        side=Side.LONG,
        limit_price=Decimal(93),
        stop_loss=Decimal(78),
        annul_close=Decimal(100),
    )
    assert levels.risk == Decimal(15)


def test_the_force_entry_is_mirrored_for_a_sell() -> None:
    levels = HammerForceTrigger().levels_for(
        flip(FORCE_HAMMER), flip(FORCE_BAR), side=Side.SHORT, tick=TICK
    )
    assert levels == HammerForceLevels(
        side=Side.SHORT,
        limit_price=Decimal(97),
        stop_loss=Decimal(112),
        annul_close=Decimal(90),
    )
    assert levels.risk == Decimal(15)


def test_the_force_stop_is_measured_off_the_hammer_and_not_off_the_force_bar() -> None:
    """⚠️ **The separating fixture for the one field two bars could both plausibly own.**

    The force bar chooses where to get in; the hammer is what the trade is measured against. Here
    the two bars have different heights on purpose — twenty percent of the hammer is 2.00 and
    twenty percent of the force bar is 1.90 — so an implementation that reached for the wrong bar
    lands on a different, entirely plausible number instead of failing loudly.
    """
    levels = HammerForceTrigger().levels_for(FORCE_HAMMER, FORCE_BAR, side=Side.LONG, tick=TICK)
    assert levels is not None
    assert (FORCE_HAMMER.high - FORCE_HAMMER.low) != (FORCE_BAR.high - FORCE_BAR.low)
    assert levels.stop_loss == FORCE_HAMMER.low - Decimal("0.20") * Decimal(10)
    assert levels.stop_loss != FORCE_BAR.low - Decimal("0.20") * (FORCE_BAR.high - FORCE_BAR.low)


def test_the_force_entry_is_silent_when_the_force_bar_does_not_clear_the_hammer() -> None:
    """⚠️ **The case his rule does not describe, and the reason it needs a decision.**

    A hammer may carry an upper shadow of nearly half its height, so a perfectly valid force bar
    can top out *below* the hammer's high. "Between the two highs" then names an empty interval,
    and thirty percent of a negative spread would put a **buy limit below the hammer** — an order
    at a price his method never mentions. Refusing costs at worst a setup; arming invents one.
    """
    lower = candle(1, open_="86.5", high="89", low="86", close="88.8")
    assert is_force_bar(lower, side=Side.LONG)
    assert lower.high < FORCE_HAMMER.high
    assert HammerForceTrigger().levels_for(FORCE_HAMMER, lower, side=Side.LONG, tick=TICK) is None
    assert (
        HammerForceTrigger().levels_for(flip(FORCE_HAMMER), flip(lower), side=Side.SHORT, tick=TICK)
        is None
    )


def test_the_force_limit_lands_on_the_grid_the_costly_way_on_both_sides() -> None:
    """⚠️ **Thirty percent of ten is three, and three is on every grid** — which is why his own
    worked example cannot see this, and why the mutant swapping the rounding survived it.

    A hammer topping at 10.00 and a force bar at 10.35 put the raw limit at 10.105 on a nickel
    grid. Up to 10.15 for a buy: a worse price than the arithmetic asked for, which is the rule
    every level here follows. The sell mirror rounds the other way for the same reason, and it is
    asserted here because the buy assertion alone has never once caught the sell bug.
    """
    ham = candle(0, open_="9.85", high="10.00", low="9.60", close="9.90")
    force = candle(1, open_="10.10", high="10.35", low="10.05", close="10.32")
    nickel = Decimal("0.05")

    buy = HammerForceTrigger().levels_for(ham, force, side=Side.LONG, tick=nickel)
    assert buy is not None
    assert buy.limit_price == Decimal("10.15")  # 10.00 + 0.30 x 0.35 = 10.105, up
    assert buy.stop_loss == Decimal("9.50")  # 9.60 - 0.20 x 0.40 = 9.52, down

    sell = HammerForceTrigger().levels_for(flip(ham), flip(force), side=Side.SHORT, tick=nickel)
    assert sell is not None
    assert sell.limit_price == Decimal("179.85")
    assert sell.stop_loss == Decimal("180.50")


def test_the_force_entry_is_silent_when_the_two_highs_are_the_same_price() -> None:
    """⚠️ **The boundary, and `<` instead of `<=` survives every other test in this file.**

    A force bar topping out at exactly the hammer's high leaves an interval of zero width. Thirty
    percent of nothing is nothing, so the "pullback entry" would be a buy limit sitting **on** the
    hammer's high — not between the two extremes, which is the only place his rule puts it. The
    strict-inequality reading arms there and calls it a trade.
    """
    same_high = candle(1, open_="87", high="90", low="86.5", close="89.5")
    assert is_force_bar(same_high, side=Side.LONG)
    assert same_high.high == FORCE_HAMMER.high
    trigger = HammerForceTrigger()
    assert trigger.levels_for(FORCE_HAMMER, same_high, side=Side.LONG, tick=TICK) is None
    assert (
        trigger.levels_for(flip(FORCE_HAMMER), flip(same_high), side=Side.SHORT, tick=TICK) is None
    )


def test_the_force_entry_needs_both_bars_and_says_so_by_staying_silent() -> None:
    """Two `None`s with different causes, and both are reachable on real data: the hammer that is
    not a hammer, and the second bar that is not a force bar."""
    trigger = HammerForceTrigger()
    assert trigger.levels_for(FORCE_BAR, FORCE_BAR, side=Side.LONG, tick=TICK) is None
    assert trigger.levels_for(FORCE_HAMMER, FORCE_HAMMER, side=Side.LONG, tick=TICK) is None


# --------------------------------------------------------------------------- #
# The dials refuse values that would silently change the setup                 #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"break_ticks": 0}, "at least one tick"),
        ({"stop_fraction": Decimal(0)}, "sits past the hammer"),
        ({"shadow_fraction": Decimal(1)}, "fraction of the bar"),
    ],
)
def test_a_break_trigger_refuses_a_dial_that_would_not_be_a_setup(
    kwargs: dict[str, object], message: str
) -> None:
    """Loud rather than defaulting, the same doctrine as the VWAP triggers. `break_ticks=0` puts
    the order *on* the high rather than past it; a shadow fraction of 1 can never be exceeded, so
    the setup would arm on nothing at all — and both would run in silence."""
    with pytest.raises(EngineError, match=message):
        HammerBreakTrigger(**kwargs)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"entry_fraction": Decimal(0)}, "between the two extremes"),
        ({"entry_fraction": Decimal(1)}, "between the two extremes"),
        ({"stop_fraction": Decimal("-0.1")}, "sits past the hammer"),
    ],
)
def test_a_force_trigger_refuses_a_dial_that_would_not_be_a_setup(
    kwargs: dict[str, object], message: str
) -> None:
    """`0` puts the limit on the hammer's high and `1` puts it on the force bar's, and neither is
    "between" — at `1` the order rests where the market just was and fills on any wick."""
    with pytest.raises(EngineError, match=message):
        HammerForceTrigger(**kwargs)  # type: ignore[arg-type]


def test_the_thresholds_are_dials_rather_than_constants_in_the_comparison() -> None:
    """A bar that is not a hammer at fifty percent is one at forty, and the trigger has to follow
    the dial it was given — otherwise the parameter exists and does nothing, which is the shape of
    promise this project has been bitten by before."""
    halved = candle(0, open_="95", high="100", low="90", close="96")
    assert not is_hammer(halved, side=Side.LONG)
    assert is_hammer(halved, side=Side.LONG, shadow_fraction=Decimal("0.40"))

    lenient = Decimal("0.40")
    levels = HammerBreakTrigger(shadow_fraction=lenient).levels_for(
        halved, side=Side.LONG, tick=TICK
    )
    assert levels is not None
    assert HammerBreakTrigger().levels_for(halved, side=Side.LONG, tick=TICK) is None

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
    ForceFollowLevels,
    GiftStop,
    GiftTrigger,
    HammerBreakLevels,
    HammerBreakTrigger,
    HammerForceLevels,
    HammerForceTrigger,
    IgnoredBarTrigger,
    is_force_bar,
    is_gift,
    is_hammer,
    is_ignored_bar,
    volume_of,
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
    """His rule, given when the case was put to him: *"barra de força que não supera o martelo
    cancela entrada"* (2026-09-06).

    A hammer may carry an upper shadow of nearly half its height, so a perfectly valid force bar
    can top out *below* the hammer's high. "Between the two highs" then names an empty interval,
    and thirty percent of a negative spread would put a **buy limit below the hammer** — an order
    at a price his method never mentions.
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


def test_the_force_entry_is_silent_when_the_limit_would_be_above_the_market() -> None:
    """⚠️ **The blocking finding, and it took the whole backtest down rather than moving a number.**

    A force bar owes only seventy percent of itself to its body; the other thirty can be a wick
    through the hammer's high with a close back *under* the thirty-percent level. Here the hammer
    tops at 102 and the force bar wicks to 102.10 and closes at 102.00 — ninety percent body, a
    perfectly ordinary strong candle spiking resistance and finishing on it. Thirty percent of the
    ten-cent spread puts the limit at **102.03, above the close it would rest against**.

    That is not a pullback entry. `Signal` refuses a buy limit above the market as the sign error
    it almost always is, and nothing in `loop.py` catches a `ValueError` — so the session dies on
    an ordinary bar. Refusing here is the same ending the two refusals above give.
    """
    hammer = candle(0, open_="101", high="102", low="98", close="101.5")
    spikes_and_closes_back = candle(1, open_="99.20", high="102.10", low="99.00", close="102.00")

    assert is_force_bar(spikes_and_closes_back, side=Side.LONG)
    assert spikes_and_closes_back.high > hammer.high  # it does clear the hammer
    # 102 + 0.30 x 0.10 = 102.03, which is past the 102.00 the bar closed at.
    assert (
        HammerForceTrigger().levels_for(hammer, spikes_and_closes_back, side=Side.LONG, tick=PENNY)
        is None
    )
    assert (
        HammerForceTrigger().levels_for(
            flip(hammer), flip(spikes_and_closes_back), side=Side.SHORT, tick=PENNY
        )
        is None
    )


def test_a_force_bar_that_closes_clear_of_the_limit_still_arms() -> None:
    """The other side of the guard, so "wrong side of the market" cannot quietly become "never".

    Same hammer, a force bar that closes at 104 with the limit at 102.75 — a real pullback entry,
    well below where the market finished.
    """
    hammer = candle(0, open_="101", high="102", low="98", close="101.5")
    force = candle(1, open_="101.6", high="104.5", low="101.4", close="104")

    levels = HammerForceTrigger().levels_for(hammer, force, side=Side.LONG, tick=PENNY)
    assert levels is not None
    assert levels.limit_price == Decimal("102.75")
    assert levels.limit_price < force.close


# --------------------------------------------------------------------------- #
# The gift and the ignored bar                                                 #
# --------------------------------------------------------------------------- #

# His force bar off a demand region, and the two followers he dictated on it (2026-09-07).
# The force bar is 6.50 tall with a 5.50 body; a third of it is 2.1666..., which is exactly the
# number a `Decimal` fraction cannot hold — see `DEFAULT_GIFT_RANGE_DIVISOR`.
GIFT_FORCE = candle(0, open_="100", high="106", low="99.50", close="105.50")
GIFT = candle(1, open_="105.20", high="105.60", low="104.40", close="105.00")
IGNORED = candle(1, open_="105.50", high="105.80", low="102.00", close="102.50")


def loud(bar: Candle, *, ticks: int = 0, real: int = 0) -> Candle:
    """The same bar carrying volume — the fixtures above carry none, on purpose, so that the
    filter's answer to "no volume" is a separate assertion rather than an accident."""
    return Candle(
        time=bar.time,
        open=bar.open,
        high=bar.high,
        low=bar.low,
        close=bar.close,
        tick_volume=ticks,
        real_volume=real,
    )


def test_a_gift_is_a_small_bar_in_the_force_bar_s_upper_third() -> None:
    assert is_gift(GIFT_FORCE, GIFT, side=Side.LONG)
    assert is_gift(flip(GIFT_FORCE), flip(GIFT), side=Side.SHORT)


def test_a_gift_may_close_either_way() -> None:
    """He gave the gift's size and its place, and nothing about its colour. A small red bar in
    the upper third is a gift — the mutant demanding `close > open` here would be inventing a
    rule he did not."""
    red = candle(1, open_="105.50", high="105.60", low="104.40", close="104.60")
    assert red.close < red.open
    assert is_gift(GIFT_FORCE, red, side=Side.LONG)
    assert is_gift(flip(GIFT_FORCE), flip(red), side=Side.SHORT)


def test_a_bar_taller_than_a_third_of_the_force_bar_is_not_a_gift() -> None:
    """2.20 against a third of 6.50 — over by a few ticks, and in the upper third."""
    tall = candle(1, open_="105.20", high="106.00", low="103.80", close="105.00")
    assert (tall.high - tall.low) * 3 > (GIFT_FORCE.high - GIFT_FORCE.low)
    assert not is_gift(GIFT_FORCE, tall, side=Side.LONG)
    assert not is_gift(flip(GIFT_FORCE), flip(tall), side=Side.SHORT)


def test_a_small_bar_in_the_middle_of_the_force_bar_is_not_a_gift() -> None:
    """*"Nunca no meio ou na mínima."* Small enough, wrong place: a bar that would be a gift by
    size sits with its low at 102.50, below the line a third down from 106 (103.83)."""
    middle = candle(1, open_="103", high="103.50", low="102.50", close="103.20")
    assert not is_gift(GIFT_FORCE, middle, side=Side.LONG)
    assert not is_gift(flip(GIFT_FORCE), flip(middle), side=Side.SHORT)


def test_a_gift_of_exactly_a_third_with_its_low_exactly_on_the_line_is_a_gift() -> None:
    """⚠️ **The boundary the `Decimal` fraction gets wrong, on both edges at once.**

    A three-point force bar makes a third exactly one point. A gift one point tall whose low sits
    exactly one point under the force bar's high is *no máximo um terço* and *no terço superior*
    — both by his words — and `Decimal(1) / Decimal(3)` would refuse it on both counts, because
    `0.3333… x 3 < 1`. Written as a multiplication there is no rounding to lose it to.
    """
    force = candle(0, open_="100", high="103", low="100", close="102.50")
    on_the_line = candle(1, open_="102.20", high="103", low="102", close="102.50")
    assert (on_the_line.high - on_the_line.low) * 3 == force.high - force.low
    assert is_gift(force, on_the_line, side=Side.LONG)
    assert is_gift(flip(force), flip(on_the_line), side=Side.SHORT)


def test_a_gift_straddling_the_line_of_the_third_is_not_a_gift() -> None:
    """⚠️ **The fixture that separates "gift inteiro" from "the gift's high".** Asked whether the
    upper third holds the whole gift or only its top, he answered *"gift inteiro"*. Every other
    gift fixture here either sits wholly above the line or wholly below it, so a reading that
    tested the **high** against the line passed the suite on both sides — the guardian's finding.

    Small enough (1.10 against a third of 6.50), its high at 104.60 above the line at 103.83 and
    its low at 103.50 under it: the high-only reading arms a stop order at 106.01 that his method
    does not recognise, and the backtest simply becomes more active.
    """
    straddles = candle(1, open_="103.60", high="104.60", low="103.50", close="104.40")
    line = GIFT_FORCE.high - (GIFT_FORCE.high - GIFT_FORCE.low) / 3
    assert straddles.low < line < straddles.high
    assert not is_gift(GIFT_FORCE, straddles, side=Side.LONG)
    assert not is_gift(flip(GIFT_FORCE), flip(straddles), side=Side.SHORT)


def test_a_gift_may_reach_above_the_force_bar_s_high() -> None:
    """The upper third has a floor and no ceiling: a gift topping above the force bar is still
    entirely in the upper third, and its high is then where the entry measures from."""
    higher = candle(1, open_="105.60", high="106.30", low="105.20", close="105.90")
    assert higher.high > GIFT_FORCE.high
    assert is_gift(GIFT_FORCE, higher, side=Side.LONG)
    assert is_gift(flip(GIFT_FORCE), flip(higher), side=Side.SHORT)


def test_an_ignored_bar_is_a_real_body_that_kept_the_force_bar_s_low() -> None:
    assert is_ignored_bar(GIFT_FORCE, IGNORED, side=Side.LONG)
    assert is_ignored_bar(flip(GIFT_FORCE), flip(IGNORED), side=Side.SHORT)


def test_an_ignored_bar_may_close_either_way() -> None:
    """The rule as given is a body over a third that kept the low. Whether the colour should be
    held is an open question in the backlog; until he answers, a green bar qualifies too."""
    green = candle(1, open_="102.50", high="105.80", low="102.00", close="105.50")
    assert green.close > green.open
    assert is_ignored_bar(GIFT_FORCE, green, side=Side.LONG)
    assert is_ignored_bar(flip(GIFT_FORCE), flip(green), side=Side.SHORT)


def test_a_body_of_exactly_a_third_is_not_an_ignored_bar() -> None:
    """*"Corpo maior que 1/3"* — strictly. A three-point force bar and a one-point body: not an
    ignored bar, and not a gift either if its range is over a point. The gap between the two is
    real and this bar is in it."""
    force = candle(0, open_="100", high="103", low="100", close="102.50")
    a_third = candle(1, open_="102", high="102.50", low="100.80", close="101")
    assert abs(a_third.close - a_third.open) * 3 == force.high - force.low
    assert not is_ignored_bar(force, a_third, side=Side.LONG)
    assert not is_ignored_bar(flip(force), flip(a_third), side=Side.SHORT)
    assert not is_gift(force, a_third, side=Side.LONG)

    over = candle(1, open_="102.01", high="102.50", low="100.80", close="101")
    assert is_ignored_bar(force, over, side=Side.LONG)
    assert is_ignored_bar(flip(force), flip(over), side=Side.SHORT)


def test_a_bar_that_broke_the_force_bar_s_low_is_not_an_ignored_bar() -> None:
    """One tick under 99.50 is broken; sitting exactly on it is not — the same reading the
    region's own edge gets in `_RegionWatch.broke`."""
    broke = candle(1, open_="105.50", high="105.80", low="99.49", close="102.50")
    assert not is_ignored_bar(GIFT_FORCE, broke, side=Side.LONG)
    assert not is_ignored_bar(flip(GIFT_FORCE), flip(broke), side=Side.SHORT)

    on_it = candle(1, open_="105.50", high="105.80", low="99.50", close="102.50")
    assert is_ignored_bar(GIFT_FORCE, on_it, side=Side.LONG)
    assert is_ignored_bar(flip(GIFT_FORCE), flip(on_it), side=Side.SHORT)


def test_no_bar_is_both_a_gift_and_an_ignored_bar() -> None:
    """A gift is at most a third tall, so its body is at most a third; an ignored bar's body is
    over a third. His two followers are each exactly one of the two."""
    assert not is_ignored_bar(GIFT_FORCE, GIFT, side=Side.LONG)
    assert not is_gift(GIFT_FORCE, IGNORED, side=Side.LONG)


def test_the_gift_entry_waits_one_tick_past_the_higher_high_and_stops_off_the_chosen_bar() -> None:
    """His example, read off the trigger: order at 106.01; the stop 104.16 off the gift (a fifth
    of its 1.20 under 104.40) or 98.20 off the force bar (a fifth of its 6.50 under 99.50)."""
    off_gift = GiftTrigger().levels_for(GIFT_FORCE, GIFT, side=Side.LONG, tick=PENNY)
    assert off_gift == ForceFollowLevels(
        side=Side.LONG,
        stop_price=Decimal("106.01"),
        stop_loss=Decimal("104.16"),
        annul_price=Decimal("99.50"),
    )
    assert off_gift.risk == Decimal("1.85")

    off_force = GiftTrigger(stop_at=GiftStop.FORCA).levels_for(
        GIFT_FORCE, GIFT, side=Side.LONG, tick=PENNY
    )
    assert off_force == ForceFollowLevels(
        side=Side.LONG,
        stop_price=Decimal("106.01"),
        stop_loss=Decimal("98.20"),
        annul_price=Decimal("99.50"),
    )


def test_the_gift_entry_is_mirrored_for_a_sell() -> None:
    off_gift = GiftTrigger().levels_for(flip(GIFT_FORCE), flip(GIFT), side=Side.SHORT, tick=PENNY)
    assert off_gift == ForceFollowLevels(
        side=Side.SHORT,
        stop_price=Decimal("83.99"),
        stop_loss=Decimal("85.84"),
        annul_price=Decimal("90.50"),
    )
    off_force = GiftTrigger(stop_at=GiftStop.FORCA).levels_for(
        flip(GIFT_FORCE), flip(GIFT), side=Side.SHORT, tick=PENNY
    )
    assert off_force == ForceFollowLevels(
        side=Side.SHORT,
        stop_price=Decimal("83.99"),
        stop_loss=Decimal("91.80"),
        annul_price=Decimal("90.50"),
    )


def test_the_annulment_is_the_force_bar_s_low_whichever_bar_the_stop_is_measured_from() -> None:
    """His second dictation, when asked what takes the resting order back: *"se o preço perder a
    mínima da barra de força anula"*. Both triggers, both stops, both sides: the level is the force
    bar's extreme and never the gift's — and with the stop off the gift it sits **below** the stop,
    so the two are not the same kind of number even when the force-bar stop puts them a fifth of a
    bar apart."""
    for trigger in (GiftTrigger(), GiftTrigger(stop_at=GiftStop.FORCA)):
        buy = trigger.levels_for(GIFT_FORCE, GIFT, side=Side.LONG, tick=PENNY)
        assert buy is not None
        assert buy.annul_price == GIFT_FORCE.low
        assert buy.annul_price != GIFT.low
        assert buy.annul_price != buy.stop_loss
        sell = trigger.levels_for(flip(GIFT_FORCE), flip(GIFT), side=Side.SHORT, tick=PENNY)
        assert sell is not None
        assert sell.annul_price == flip(GIFT_FORCE).high

    ignored = IgnoredBarTrigger().levels_for(GIFT_FORCE, IGNORED, side=Side.LONG, tick=PENNY)
    assert ignored is not None
    assert ignored.annul_price == GIFT_FORCE.low
    assert ignored.annul_price != IGNORED.low


def test_the_two_gift_stops_are_different_numbers_on_his_own_example() -> None:
    """⚠️ The separating fixture for the dial: the gift is 1.20 tall and the force bar 6.50, so
    reaching for the wrong bar lands on the other plausible number rather than on an error."""
    off_gift = GiftTrigger(stop_at=GiftStop.GIFT).levels_for(
        GIFT_FORCE, GIFT, side=Side.LONG, tick=PENNY
    )
    off_force = GiftTrigger(stop_at=GiftStop.FORCA).levels_for(
        GIFT_FORCE, GIFT, side=Side.LONG, tick=PENNY
    )
    assert off_gift is not None
    assert off_force is not None
    assert off_gift.stop_loss == GIFT.low - Decimal("0.20") * (GIFT.high - GIFT.low)
    assert off_force.stop_loss == GIFT_FORCE.low - Decimal("0.20") * (
        GIFT_FORCE.high - GIFT_FORCE.low
    )
    assert off_gift.stop_loss != off_force.stop_loss


def test_the_entry_measures_from_the_gift_when_the_gift_is_the_higher_bar() -> None:
    """*"A máxima mais alta entre a barra de força e o gift."* Gift topping at 106.30 over the
    force bar's 106: the order at 106.31, not 106.01."""
    higher = candle(1, open_="105.60", high="106.30", low="105.20", close="105.90")
    buy = GiftTrigger().levels_for(GIFT_FORCE, higher, side=Side.LONG, tick=PENNY)
    assert buy is not None
    assert buy.stop_price == Decimal("106.31")
    sell = GiftTrigger().levels_for(flip(GIFT_FORCE), flip(higher), side=Side.SHORT, tick=PENNY)
    assert sell is not None
    assert sell.stop_price == Decimal("83.69")


def test_the_gift_levels_land_on_the_grid_the_costly_way_on_both_sides() -> None:
    """Force bar topping at 10.33 on a nickel grid: the order at 10.38 raw goes **up** to 10.40;
    the gift stop at 10.238 raw goes **down** to 10.20. The sell mirror rounds the other way, and
    is asserted because the buy assertion alone has never once caught the sell bug."""
    force = candle(0, open_="10.10", high="10.33", low="10.05", close="10.30")
    gift = candle(1, open_="10.27", high="10.31", low="10.25", close="10.29")
    nickel = Decimal("0.05")

    buy = GiftTrigger().levels_for(force, gift, side=Side.LONG, tick=nickel)
    assert buy == ForceFollowLevels(
        side=Side.LONG,
        stop_price=Decimal("10.40"),
        stop_loss=Decimal("10.20"),
        annul_price=Decimal("10.05"),  # the force bar's low, as traded: never snapped to the grid
    )
    off_force = GiftTrigger(stop_at=GiftStop.FORCA).levels_for(
        force, gift, side=Side.LONG, tick=nickel
    )
    assert off_force is not None
    assert off_force.stop_loss == Decimal("9.95")  # 10.05 - 0.20 x 0.28 = 9.994, down

    sell = GiftTrigger().levels_for(flip(force), flip(gift), side=Side.SHORT, tick=nickel)
    assert sell == ForceFollowLevels(
        side=Side.SHORT,
        stop_price=Decimal("179.60"),
        stop_loss=Decimal("179.80"),
        annul_price=Decimal("179.95"),
    )


def test_the_ignored_bar_entry_waits_past_the_higher_high_and_stops_off_the_force_bar() -> None:
    """His example: order at 106.01, stop at 98.20 — the force bar's, the only one it has."""
    buy = IgnoredBarTrigger().levels_for(GIFT_FORCE, IGNORED, side=Side.LONG, tick=PENNY)
    assert buy == ForceFollowLevels(
        side=Side.LONG,
        stop_price=Decimal("106.01"),
        stop_loss=Decimal("98.20"),
        annul_price=Decimal("99.50"),
    )
    sell = IgnoredBarTrigger().levels_for(
        flip(GIFT_FORCE), flip(IGNORED), side=Side.SHORT, tick=PENNY
    )
    assert sell == ForceFollowLevels(
        side=Side.SHORT,
        stop_price=Decimal("83.99"),
        stop_loss=Decimal("91.80"),
        annul_price=Decimal("90.50"),
    )


def test_the_ignored_bar_entry_measures_from_the_follower_when_it_is_higher() -> None:
    taller = candle(1, open_="105.50", high="106.40", low="102.00", close="102.50")
    buy = IgnoredBarTrigger().levels_for(GIFT_FORCE, taller, side=Side.LONG, tick=PENNY)
    assert buy is not None
    assert buy.stop_price == Decimal("106.41")
    sell = IgnoredBarTrigger().levels_for(
        flip(GIFT_FORCE), flip(taller), side=Side.SHORT, tick=PENNY
    )
    assert sell is not None
    assert sell.stop_price == Decimal("83.59")


def test_each_follower_trigger_is_silent_on_the_other_s_bar_and_on_a_missing_force_bar() -> None:
    """Four `None`s, each reachable on real data: the gift handed an ignored bar, the ignored bar
    handed a gift, and either handed a first bar that is not a bar of force."""
    assert GiftTrigger().levels_for(GIFT_FORCE, IGNORED, side=Side.LONG, tick=PENNY) is None
    assert IgnoredBarTrigger().levels_for(GIFT_FORCE, GIFT, side=Side.LONG, tick=PENNY) is None
    assert GiftTrigger().levels_for(GIFT, GIFT, side=Side.LONG, tick=PENNY) is None
    assert IgnoredBarTrigger().levels_for(IGNORED, IGNORED, side=Side.LONG, tick=PENNY) is None


# --------------------------------------------------------------------------- #
# The volume filter                                                            #
# --------------------------------------------------------------------------- #


def test_the_volume_filter_admits_a_follower_at_seventy_percent_and_refuses_one_tick_over() -> None:
    """*"No máximo 70% do volume da barra de força"* — 700 against 1000 passes, 701 does not,
    and the same line holds for the ignored bar (*"idem"*)."""
    force = loud(GIFT_FORCE, ticks=1000)
    trigger = GiftTrigger(volume_fraction=Decimal("0.70"))
    assert trigger.levels_for(force, loud(GIFT, ticks=700), side=Side.LONG, tick=PENNY)
    assert trigger.levels_for(force, loud(GIFT, ticks=701), side=Side.LONG, tick=PENNY) is None

    ignored = IgnoredBarTrigger(volume_fraction=Decimal("0.70"))
    assert ignored.levels_for(force, loud(IGNORED, ticks=700), side=Side.LONG, tick=PENNY)
    assert ignored.levels_for(force, loud(IGNORED, ticks=701), side=Side.LONG, tick=PENNY) is None


def test_the_volume_ceiling_is_the_dial_and_not_the_constant() -> None:
    """At half instead of seventy percent, 600 against 1000 is refused. A test at the default
    value alone cannot tell "the dial arrived" from "the constant was used"."""
    force = loud(GIFT_FORCE, ticks=1000)
    half = GiftTrigger(volume_fraction=Decimal("0.50"))
    assert half.levels_for(force, loud(GIFT, ticks=600), side=Side.LONG, tick=PENNY) is None
    assert half.levels_for(force, loud(GIFT, ticks=500), side=Side.LONG, tick=PENNY)


def test_the_filter_off_ignores_volume_entirely() -> None:
    """A gift louder than its force bar arms with the filter off — the fixtures at the top of
    this section carry no volume at all and arm, which is the same fact from the other side."""
    force = loud(GIFT_FORCE, ticks=100)
    assert GiftTrigger().levels_for(force, loud(GIFT, ticks=5000), side=Side.LONG, tick=PENNY)


def test_the_filter_on_fails_closed_when_the_force_bar_reports_no_volume() -> None:
    """⚠️ Zero against zero must not pass. With the filter on and a feed that carries no volume,
    the arithmetic says the follower's nothing is within seventy percent of the force bar's
    nothing, and the filter would be on and rejecting nobody. Refusing makes the missing feed
    show up as a setup that never arms."""
    silent = GiftTrigger(volume_fraction=Decimal("0.70"))
    assert silent.levels_for(GIFT_FORCE, GIFT, side=Side.LONG, tick=PENNY) is None
    assert (
        IgnoredBarTrigger(volume_fraction=Decimal("0.70")).levels_for(
            GIFT_FORCE, IGNORED, side=Side.LONG, tick=PENNY
        )
        is None
    )


def test_the_filter_reads_real_volume_where_the_venue_reports_it_and_ticks_elsewhere() -> None:
    """The VWAP's `auto` rule, so a document carrying both reads one number for volume. A bar
    with real volume is judged on it even when its tick count would say the opposite."""
    assert volume_of(loud(GIFT, ticks=900, real=0)) == 900
    assert volume_of(loud(GIFT, ticks=900, real=300)) == 300

    force = loud(GIFT_FORCE, ticks=1000, real=1000)
    trigger = GiftTrigger(volume_fraction=Decimal("0.70"))
    # Ticks would refuse this gift (900 > 700); real volume admits it (300 <= 700).
    assert trigger.levels_for(force, loud(GIFT, ticks=900, real=300), side=Side.LONG, tick=PENNY)
    # And the other way round: quiet in ticks, loud in real volume, refused.
    assert (
        trigger.levels_for(force, loud(GIFT, ticks=100, real=800), side=Side.LONG, tick=PENNY)
        is None
    )


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"range_divisor": 0}, "whole divisions"),
        ({"upper_divisor": 0}, "whole divisions"),
        ({"volume_fraction": Decimal(0)}, "positive fraction"),
        ({"break_ticks": 0}, "at least one tick"),
        ({"stop_fraction": Decimal(0)}, "sits past the bar"),
    ],
)
def test_a_gift_trigger_refuses_a_dial_that_would_not_be_a_setup(
    kwargs: dict[str, object], message: str
) -> None:
    with pytest.raises(EngineError, match=message):
        GiftTrigger(**kwargs)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"body_divisor": 0}, "whole divisions"),
        ({"volume_fraction": Decimal("-0.1")}, "positive fraction"),
        ({"break_ticks": 0}, "at least one tick"),
        ({"stop_fraction": Decimal(0)}, "sits past the bar"),
    ],
)
def test_an_ignored_bar_trigger_refuses_a_dial_that_would_not_be_a_setup(
    kwargs: dict[str, object], message: str
) -> None:
    with pytest.raises(EngineError, match=message):
        IgnoredBarTrigger(**kwargs)  # type: ignore[arg-type]


def test_the_follower_thresholds_are_dials_rather_than_constants_in_the_comparison() -> None:
    """A gift two-fifths tall passes at a divisor of 2 and fails at 3; an ignored bar with a
    quarter body passes at a divisor of 5 and fails at 3. Same bars, different answers, so the
    number in the comparison is the one that arrived."""
    two_fifths = candle(1, open_="105.20", high="106.00", low="103.40", close="105.00")
    assert not is_gift(GIFT_FORCE, two_fifths, side=Side.LONG)
    assert is_gift(GIFT_FORCE, two_fifths, side=Side.LONG, range_divisor=2, upper_divisor=2)

    quarter = candle(1, open_="105.50", high="105.80", low="103.00", close="103.90")
    assert not is_ignored_bar(GIFT_FORCE, quarter, side=Side.LONG)
    assert is_ignored_bar(GIFT_FORCE, quarter, side=Side.LONG, body_divisor=5)

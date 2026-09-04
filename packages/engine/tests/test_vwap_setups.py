"""The formation the two VWAP triggers share, against the author's own worked example.

Every golden here is the reflection of one on the other side: the demand scenario is mirrored
around 190, so a bug that reads `low` where it should read `high` does not merely change a number,
it produces the *other side's* number. That matters because the silent mirror has been the
blocking finding on the last two engine PRs in a row — both times because only the long side had
a scenario, and both times the sell-side fixture happened to coincide.

The scenario is his, from 2026-09-04: a demand region of [90, 100], price falling into it, the
deepest bar at 95.00, and an up bar that confirms. The numbers were read off the real
`AnchoredVWAP` before they were written here, never worked out by hand and never guessed.
"""

import logging
from decimal import Decimal, localcontext

import pytest

from tradeforge_engine.domain import Candle, Money, Side
from tradeforge_engine.errors import EngineError
from tradeforge_engine.loop import ENGINE_CONTEXT
from tradeforge_engine.structure import OrderBlock, StructureKind, ZoneKind
from tradeforge_engine.testing import START, bar
from tradeforge_engine.vwap_setups import (
    BotinhaOrder,
    BotinhaTrigger,
    FormationState,
    VwapFormation,
    VwapLines,
)


def _region(kind: ZoneKind, *, top: str = "100", bottom: str = "90") -> OrderBlock:
    """The author's standing example region, [90, 100], on either side of the book.

    The bounds are overridable for one reason only: the guard against a level falling through
    zero needs a band tall relative to its own price, which [90, 100] cannot produce.
    """
    return OrderBlock(
        kind=kind,
        top=Decimal(top),
        bottom=Decimal(bottom),
        time=START,
        confirmed_at=START,
        break_kind=StructureKind.BOS,
        primary=True,
    )


def _run(formation: VwapFormation, candles: list[Candle]) -> VwapLines | None:
    with localcontext(ENGINE_CONTEXT):
        for candle in candles:
            formation.update(candle)
        return formation.lines()


# His example, bar for bar. Bar 1 enters the region and closes down; bar 2 falls further and makes
# the 95.00 that becomes the anchor; bar 3 closes up and confirms.
_INTO_DEMAND = [
    bar(1, open_="100.50", high="100.60", low="98.00", close="98.50", tick_volume=1000),
    bar(2, open_="98.50", high="98.80", low="95.00", close="95.50", tick_volume=1500),
    bar(3, open_="95.50", high="97.50", low="95.20", close="97.20", tick_volume=1200),
]

# The same three bars reflected around 190: every price p becomes 190 - p, so highs and lows swap
# roles and a falling reaction becomes a rising one into a supply region.
_INTO_SUPPLY = [
    bar(1, open_="89.50", high="92.00", low="89.40", close="91.50", tick_volume=1000),
    bar(2, open_="91.50", high="95.00", low="91.20", close="94.50", tick_volume=1500),
    bar(3, open_="94.50", high="94.80", low="92.50", close="92.80", tick_volume=1200),
]


def test_the_anchor_is_the_bar_that_reached_deepest_not_the_one_that_confirmed() -> None:
    """Bar 2 made the 95.00; bar 3 is what told us the reaction was over.

    His words: *"ancora a VWAP na barra que fez 95 — a de mínima mais baixa, NÃO a que
    confirmou"*. Anchoring on the confirming bar is the obvious wrong reading, and it is the one
    that costs nothing to write, so the scenario is built so the two bars differ.
    """
    formation = VwapFormation(_region(ZoneKind.DEMAND))
    _run(formation, _INTO_DEMAND)

    assert formation.anchor is not None
    assert formation.anchor.time == _INTO_DEMAND[1].time
    assert formation.state is FormationState.ANCHORED


def test_the_anchor_bar_is_inside_its_own_average() -> None:
    """The golden, and it is chosen to separate the inclusive anchor from the exclusive one.

    Accumulating from bar 2 gives 96.5222…; starting at bar 3 instead gives 96.6333…. Both are
    plausible readings of "anchor the VWAP at that bar" and he settled it on 2026-09-04: the
    anchor bar counts.

    It separates a second mutant for free. Bar 1 also reached into the region, so an
    implementation that never dropped the bars before the deepest one would average three bars
    instead of two and land on neither number.
    """
    formation = VwapFormation(_region(ZoneKind.DEMAND))
    lines = _run(formation, _INTO_DEMAND)

    assert lines is not None
    # Asserted as text, not as Decimal: 96.5222 and 96.52220000 compare equal numerically, so a
    # precision that quietly changed would pass a numeric assertion unnoticed.
    assert str(lines.vwap) == "96.52222222222222222222222222"
    assert str(lines.botinha) == "95.08888888888888888888888889"


def test_the_botinha_is_the_low_side_of_the_band_under_a_demand_region() -> None:
    """The centre is `hlc3` and the *botinha* is the `low` line, so one sits under the other."""
    formation = VwapFormation(_region(ZoneKind.DEMAND))
    lines = _run(formation, _INTO_DEMAND)

    assert lines is not None
    assert lines.botinha < lines.vwap
    assert formation.side is Side.LONG


def test_the_botinha_is_the_high_side_of_the_band_over_a_supply_region() -> None:
    """The mirror, and it is a mirror down to the last digit.

    Reflecting the demand scenario around 190 must reflect both lines: 190 - 96.52222... is exactly
    the supply centre, and 190 - 95.08888... exactly its *botinha*. An implementation that read
    `low` on both sides would put the supply *botinha* at 93.03…, under its centre instead of
    over it — which is the shape of the bug that survived two PRs here.
    """
    formation = VwapFormation(_region(ZoneKind.SUPPLY))
    lines = _run(formation, _INTO_SUPPLY)

    assert lines is not None
    assert str(lines.vwap) == "93.47777777777777777777777778"
    assert str(lines.botinha) == "94.91111111111111111111111111"
    assert lines.botinha > lines.vwap
    assert formation.side is Side.SHORT
    assert formation.anchor is not None
    assert formation.anchor.time == _INTO_SUPPLY[1].time


def test_the_confirming_bar_becomes_the_anchor_when_it_reached_deepest() -> None:
    """His correction on 2026-09-04, and it reverses my first reading of his own sentence.

    "The lowest low, not the bar that confirmed" reads as *excluding* the confirming bar. It does
    not: if the up bar is itself the one that reached furthest, the anchor goes on it. Here bar 3
    wicks to 94.60, under bar 2's 95.00, and closes up — so the average is that one bar alone and
    the *botinha* is exactly its low.
    """
    deeper_confirmation = [
        _INTO_DEMAND[0],
        _INTO_DEMAND[1],
        bar(3, open_="95.50", high="97.50", low="94.60", close="97.20", tick_volume=1200),
    ]
    formation = VwapFormation(_region(ZoneKind.DEMAND))
    lines = _run(formation, deeper_confirmation)

    assert formation.anchor is not None
    assert formation.anchor.time == deeper_confirmation[2].time
    assert lines is not None
    assert lines.botinha == Decimal("94.60")


def test_a_bar_that_left_the_region_is_still_in_the_average() -> None:
    """The accumulation runs from the anchor forward, not "every bar inside the region".

    Bar 2 here trades entirely above the 100 top and closes down, so it is no candidate for the
    anchor — but it happened after the anchor, with two thousand of volume, and the average
    everyone who traded since the anchor paid includes it. Skipping it gives 100.7060… where the
    right answer is 101.0047…, which is the kind of wrong that no assertion about *state* would
    ever catch.
    """
    leaves_and_returns = [
        bar(1, open_="100.50", high="100.60", low="98.00", close="98.50", tick_volume=1000),
        bar(2, open_="102.00", high="102.50", low="100.50", close="101.00", tick_volume=2000),
        bar(3, open_="101.00", high="103.00", low="100.80", close="102.50", tick_volume=1200),
    ]
    region = _region(ZoneKind.DEMAND)
    formation = VwapFormation(region)
    lines = _run(formation, leaves_and_returns)

    assert formation.block is region
    assert formation.anchor is not None
    assert formation.anchor.time == leaves_and_returns[0].time
    assert lines is not None
    assert str(lines.vwap) == "101.0047619047619047619047619"
    assert str(lines.botinha) == "99.99047619047619047619047619"


def test_a_tie_on_the_deepest_bar_keeps_the_earlier_one() -> None:
    """Two bars reaching the same low is one price reached twice, and the first one reached it.

    The tie-break is invisible in the anchor's *price* — both bars low at 95.00 — so it is read
    off the anchor's time, and off the average, which spans two bars under the earlier reading
    and one under the later.
    """
    tied = [
        bar(1, open_="98.50", high="98.80", low="95.00", close="95.50", tick_volume=1500),
        bar(2, open_="95.50", high="96.00", low="95.00", close="95.20", tick_volume=1500),
        bar(3, open_="95.20", high="97.50", low="95.20", close="97.20", tick_volume=1200),
    ]
    formation = VwapFormation(_region(ZoneKind.DEMAND))
    _run(formation, tied)

    assert formation.anchor is not None
    assert formation.anchor.time == tied[0].time


def test_a_tie_on_the_highest_bar_of_a_supply_region_keeps_the_earlier_one_too() -> None:
    """The mirror of the tie-break, and it is the one that hides best.

    A `>=` on the sell side moves the anchor to the later of the two bars: the average loses a
    bar, both lines change, and *no state is wrong anywhere* — the formation is still anchored,
    still alive, still on the right region. Only the numbers are somebody else's.
    """
    tied = [
        bar(1, open_="91.50", high="95.00", low="91.20", close="94.50", tick_volume=1500),
        bar(2, open_="94.50", high="95.00", low="94.00", close="94.80", tick_volume=1500),
        bar(3, open_="94.80", high="94.80", low="92.50", close="92.80", tick_volume=1200),
    ]
    formation = VwapFormation(_region(ZoneKind.SUPPLY))
    _run(formation, tied)

    assert formation.anchor is not None
    assert formation.anchor.time == tied[0].time


def test_a_wick_through_the_far_edge_kills_the_formation() -> None:
    """Wick, not close — his answer, asked with this exact bar.

    The bar pierces to 89.80 and closes back inside at 95.20, so a rule written on closes would
    let the formation live and anchor one bar later.
    """
    broken = [
        _INTO_DEMAND[0],
        bar(2, open_="95.50", high="95.60", low="89.80", close="95.20", tick_volume=1500),
        _INTO_DEMAND[2],
    ]
    formation = VwapFormation(_region(ZoneKind.DEMAND))
    lines = _run(formation, broken)

    assert formation.state is FormationState.DEAD
    assert formation.anchor is None
    assert lines is None


def test_a_wick_through_the_far_edge_kills_a_supply_formation_too() -> None:
    """The same rule over a supply region, where the far edge is the top and the wick goes up."""
    broken = [
        _INTO_SUPPLY[0],
        bar(2, open_="94.50", high="100.20", low="94.40", close="94.80", tick_volume=1500),
        _INTO_SUPPLY[2],
    ]
    formation = VwapFormation(_region(ZoneKind.SUPPLY))

    assert _run(formation, broken) is None
    assert formation.state is FormationState.DEAD


def test_a_wick_that_stops_on_the_far_edge_does_not_kill() -> None:
    """Touching 90 and breaking 90 are different events, and only the second ends the formation.

    Written down because the opposite is one character away, and because it decides what happens
    on the most common bar in the whole method: the one that turns exactly on the edge.
    """
    on_the_edge = [
        _INTO_DEMAND[0],
        bar(2, open_="95.50", high="95.60", low="90.00", close="95.20", tick_volume=1500),
        _INTO_DEMAND[2],
    ]
    formation = VwapFormation(_region(ZoneKind.DEMAND))
    lines = _run(formation, on_the_edge)

    assert formation.state is FormationState.ANCHORED
    assert formation.anchor is not None
    assert formation.anchor.time == on_the_edge[1].time
    assert lines is not None


def test_a_wick_that_stops_on_the_far_edge_of_a_supply_region_does_not_kill_either() -> None:
    """The same boundary over supply, where the far edge is the 100 and the wick goes up.

    Written because the demand twin alone leaves `high > top` unobservable: nothing else in the
    supply fixtures ever comes near the top, so `>` and `>=` agree on every bar of every other
    sell-side scenario, and the tighter one would kill the formation on the most common bar of the
    method — the turn exactly at the level.
    """
    on_the_edge = [
        _INTO_SUPPLY[0],
        bar(2, open_="94.50", high="100.00", low="94.40", close="94.80", tick_volume=1500),
        _INTO_SUPPLY[2],
    ]
    formation = VwapFormation(_region(ZoneKind.SUPPLY))
    lines = _run(formation, on_the_edge)

    assert formation.state is FormationState.ANCHORED
    assert formation.anchor is not None
    assert formation.anchor.time == on_the_edge[1].time
    assert lines is not None


def test_the_break_outranks_a_confirmation_on_the_same_bar() -> None:
    """One bar can pierce the far edge and still close up. His rule is unconditional, so it dies.

    Without the break being read first, this bar would anchor a formation on a region price had
    already left — and the trade that followed would be recorded as a normal one.
    """
    breaks_and_closes_up = [
        _INTO_DEMAND[0],
        bar(2, open_="95.50", high="97.00", low="89.50", close="96.80", tick_volume=1500),
    ]
    formation = VwapFormation(_region(ZoneKind.DEMAND))
    _run(formation, breaks_and_closes_up)

    assert formation.state is FormationState.DEAD
    assert formation.anchor is None


def test_the_window_is_seven_bars_counted_from_the_confirming_bar() -> None:
    """Alive on the seventh bar, dead when it closes — and the sixth proves it is not six.

    A window measured from the *anchor* rather than from the confirmation would expire one bar
    early here, because his reaction ran two bars before the up bar confirmed.
    """
    quiet = [
        bar(index, open_="97.20", high="97.40", low="96.00", close="96.30", tick_volume=900)
        for index in range(4, 12)
    ]
    formation = VwapFormation(_region(ZoneKind.DEMAND))

    with localcontext(ENGINE_CONTEXT):
        for candle in _INTO_DEMAND:
            formation.update(candle)
        assert formation.bars_left == 7

        for candle in quiet[:6]:
            formation.update(candle)
        # Read into a local before each assertion: `state` is a property, and mypy narrows a
        # property access to the literal an assert proved — so a second assert on the same
        # expression is read as a contradiction rather than as the next bar's answer.
        after_six = formation.state
        assert after_six is FormationState.ANCHORED
        assert formation.bars_left == 1

        formation.update(quiet[6])
        after_seven = formation.state
        assert after_seven is FormationState.DEAD
        assert formation.bars_left == 0


def test_the_window_is_injectable_so_its_off_by_one_can_be_reached() -> None:
    """The same rule at a window of two, where the boundary is two bars from the fixture."""
    quiet = [
        bar(index, open_="97.20", high="97.40", low="96.00", close="96.30", tick_volume=900)
        for index in range(4, 7)
    ]
    formation = VwapFormation(_region(ZoneKind.DEMAND), bars_to_trigger=2)

    with localcontext(ENGINE_CONTEXT):
        for candle in _INTO_DEMAND:
            formation.update(candle)
        formation.update(quiet[0])
        after_one = formation.state
        assert after_one is FormationState.ANCHORED
        formation.update(quiet[1])
        after_two = formation.state
        assert after_two is FormationState.DEAD


def test_a_window_of_no_bars_is_refused_rather_than_run() -> None:
    with pytest.raises(EngineError, match="at least one bar"):
        VwapFormation(_region(ZoneKind.DEMAND), bars_to_trigger=0)


def test_a_volume_source_the_indicator_does_not_know_is_refused_at_construction() -> None:
    """Both knobs fail at the same moment, and the moment is before the backtest starts.

    Left to the anchoring bar, a typo in the volume source would raise thousands of bars into a
    run — same wrong document, same wrong outcome, discovered at a point where it costs a whole
    execution to find out.
    """
    with pytest.raises(EngineError, match="unknown VWAP volume"):
        VwapFormation(_region(ZoneKind.DEMAND), volume="tik")


def test_a_formation_killed_by_its_window_stops_offering_lines() -> None:
    """Both averages survive the bar that kills the formation, and they must stop answering.

    The numbers stay perfectly valid — nothing resets them — so a trigger that read `lines()`
    without checking `state` would price an entry off a formation whose window ran out. It fails
    closed here, once, rather than in each of the two triggers that will read this.
    """
    quiet = [
        bar(index, open_="97.20", high="97.40", low="96.00", close="96.30", tick_volume=900)
        for index in range(4, 12)
    ]
    formation = VwapFormation(_region(ZoneKind.DEMAND))

    with localcontext(ENGINE_CONTEXT):
        for candle in _INTO_DEMAND:
            formation.update(candle)
        assert formation.lines() is not None
        for candle in quiet[:7]:
            formation.update(candle)
        state = formation.state
        assert state is FormationState.DEAD
        assert formation.lines() is None


def test_a_dead_formation_ignores_every_bar_after_it() -> None:
    """Nothing revives it — not a bar back inside the region, not one that would confirm."""
    formation = VwapFormation(_region(ZoneKind.DEMAND))
    with localcontext(ENGINE_CONTEXT):
        formation.update(bar(1, open_="95.50", high="95.60", low="89.80", close="95.20"))
        assert formation.state is FormationState.DEAD
        for candle in _INTO_DEMAND:
            formation.update(candle)

    assert formation.state is FormationState.DEAD
    assert formation.anchor is None


def test_a_region_price_has_not_reached_is_only_watched() -> None:
    """Bars that never touch the region start nothing, so nothing is accumulated to anchor."""
    away = [
        bar(1, open_="105", high="106", low="104", close="105.50", tick_volume=1000),
        bar(2, open_="105.50", high="107", low="105", close="106.50", tick_volume=1000),
    ]
    formation = VwapFormation(_region(ZoneKind.DEMAND))
    lines = _run(formation, away)

    assert formation.state is FormationState.WATCHING
    assert formation.anchor is None
    assert lines is None
    assert formation.bars_left == 0


def test_a_supply_region_price_has_not_reached_is_only_watched() -> None:
    """The mirror, and the worst mutant in this file dies exactly here.

    Swap the supply branch of `_entered` for the demand one and this becomes `low <= top`: price
    trading in the eighties, twenty points under a region it never came near, reads as *inside*
    it. The formation is born, anchors, and the trigger arms a sale on a region the market never
    visited. Every other supply scenario has price in the region, so all of them agree.
    """
    away = [
        bar(1, open_="85", high="86", low="84", close="85.50", tick_volume=1000),
        bar(2, open_="85.50", high="87", low="85", close="86.50", tick_volume=1000),
    ]
    formation = VwapFormation(_region(ZoneKind.SUPPLY))
    lines = _run(formation, away)

    assert formation.state is FormationState.WATCHING
    assert formation.anchor is None
    assert lines is None


def test_a_bar_that_reaches_exactly_the_entry_edge_has_entered() -> None:
    """`low <= top` is his indicator's rule verbatim, and the edge itself counts as a touch."""
    formation = VwapFormation(_region(ZoneKind.DEMAND))
    with localcontext(ENGINE_CONTEXT):
        formation.update(bar(1, open_="101", high="101.50", low="100", close="100.50"))

    assert formation.state is FormationState.FORMING


def test_a_bar_that_reaches_exactly_the_entry_edge_of_a_supply_region_has_entered() -> None:
    """`high >= bottom` over supply, the reflection of the bar above around 190.

    With `>` instead, a rally whose high stops dead on the 90 starts no formation at all on the
    sell side while its mirror image starts one on the buy side. The asymmetry never raises
    anything: the sell setup simply produces fewer trades than the method does.
    """
    formation = VwapFormation(_region(ZoneKind.SUPPLY))
    with localcontext(ENGINE_CONTEXT):
        formation.update(bar(1, open_="89", high="90", low="88.50", close="89.50"))

    assert formation.state is FormationState.FORMING


def test_a_doji_confirms_neither_side() -> None:
    """`close == open` ends no reaction: the confirmation is a close *in the trade's direction*.

    Asserted on both sides because the two comparisons are separate lines, and a `>=` on either
    of them anchors one bar early — on a bar that showed no direction at all.
    """
    demand_doji = [
        _INTO_DEMAND[0],
        bar(2, open_="98.50", high="98.80", low="95.00", close="98.50", tick_volume=1500),
    ]
    long_side = VwapFormation(_region(ZoneKind.DEMAND))
    _run(long_side, demand_doji)
    assert long_side.state is FormationState.FORMING
    assert long_side.anchor is None

    supply_doji = [
        _INTO_SUPPLY[0],
        bar(2, open_="94.50", high="94.90", low="94.20", close="94.50", tick_volume=1500),
    ]
    short_side = VwapFormation(_region(ZoneKind.SUPPLY))
    _run(short_side, supply_doji)
    assert short_side.state is FormationState.FORMING
    assert short_side.anchor is None


def test_the_same_bars_without_volume_produce_no_lines() -> None:
    """The obligatory pair, and the reason the test helper had to learn about volume.

    `AnchoredVWAP.update` skips a bar with no volume, so a scenario built from bars that carry
    none can never produce a value — and every assertion about a VWAP-triggered setup staying
    silent would pass for the wrong reason. Same bars, same region, same anchor: only the volume
    differs, and it is the difference between two lines and none.
    """
    mute = [
        bar(1, open_="100.50", high="100.60", low="98.00", close="98.50"),
        bar(2, open_="98.50", high="98.80", low="95.00", close="95.50"),
        bar(3, open_="95.50", high="97.50", low="95.20", close="97.20"),
    ]
    formation = VwapFormation(_region(ZoneKind.DEMAND))
    lines = _run(formation, mute)

    # The formation itself is healthy: it found the reaction and anchored where it should.
    assert formation.state is FormationState.ANCHORED
    assert formation.anchor is not None
    assert formation.anchor.time == mute[1].time
    # It simply has nothing to say, which is the whole failure mode this pair exists to pin.
    assert lines is None

    assert _run(VwapFormation(_region(ZoneKind.DEMAND)), _INTO_DEMAND) is not None

    # ⚠️ **And the third case is what proves the knob is wired at all.** The two above differ in
    # the *bars*, so they hold with the volume source discarded and hard-coded to `auto`. These
    # are the same bars as the working one — tick volume, no exchange volume — asked for `real`,
    # which is the shape of every series on disk but the one stock. Silence here means the
    # parameter reached the indicator; two lines mean it was thrown away and the backtest is
    # trading somebody else's numbers under his document.
    asked_for_real = VwapFormation(_region(ZoneKind.DEMAND), volume="real")
    assert _run(asked_for_real, _INTO_DEMAND) is None
    assert asked_for_real.state is FormationState.ANCHORED


def test_the_silence_for_want_of_volume_says_so_once(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """One warning per formation: enough to find it, not enough to bury the run.

    The message has to name the volume source, because the failure is nearly always a series that
    has ticks and no exchange volume being asked for `real` — measured on the 144 series on disk,
    that is every symbol but the one stock. Asking twice must still say it once: the condition is
    a property of the series, so it is true on every bar of every region in the run.
    """
    mute = [
        bar(1, open_="100.50", high="100.60", low="98.00", close="98.50"),
        bar(2, open_="98.50", high="98.80", low="95.00", close="95.50"),
        bar(3, open_="95.50", high="97.50", low="95.20", close="97.20"),
    ]
    formation = VwapFormation(_region(ZoneKind.DEMAND), volume="real")
    with localcontext(ENGINE_CONTEXT):
        for candle in mute:
            formation.update(candle)

    with caplog.at_level(logging.WARNING, logger="tradeforge_engine.vwap_setups"):
        assert formation.lines() is None
        assert formation.lines() is None

    warnings = [record for record in caplog.records if record.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "real" in warnings[0].getMessage()
    assert "demand" in warnings[0].getMessage()


def test_a_formation_that_is_not_anchored_yet_is_silent_without_complaining(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The two silences read the same: not anchored yet, and anchored over a series with no
    volume, both come back `None` from `lines()`.

    They are different statements — one is the setup working, the other is the setup broken — so
    only the second one is worth a word in the log. Nothing separates them but `state`, which is
    why this asserts on the log rather than on the return value.
    """
    formation = VwapFormation(_region(ZoneKind.DEMAND))

    with caplog.at_level(logging.WARNING, logger="tradeforge_engine.vwap_setups"):
        assert formation.lines() is None

    assert formation.state is FormationState.WATCHING
    assert [record for record in caplog.records if record.levelno == logging.WARNING] == []


# --------------------------------------------------------------------------- #
# The Botinha trigger: where the order rests, and where it dies                #
# --------------------------------------------------------------------------- #

TICK = Decimal("0.01")

# One bar that enters the region and confirms on its own, chosen so the two lines land exactly on
# the numbers he used when he dictated the rule: hlc3 = (110 + 90 + 100)/3 = 100, low = 90.
# Everything below is measured against a band of ten, which is his example's band.
_HIS_EXAMPLE = bar(1, open_="91", high="110", low="90", close="100", tick_volume=1000)

# The same bar reflected around 190: hlc3 = 90, high = 100, so the band is ten the other way up.
_HIS_EXAMPLE_MIRRORED = bar(1, open_="99", high="100", low="80", close="90", tick_volume=1000)


def _order(
    candles: list[Candle], kind: ZoneKind, *, trigger: BotinhaTrigger | None = None, **bounds: str
) -> BotinhaOrder | None:
    formation = VwapFormation(_region(kind, **bounds))
    with localcontext(ENGINE_CONTEXT):
        for candle in candles:
            formation.update(candle)
        return (trigger or BotinhaTrigger()).order_for(formation, tick=TICK, candle=candles[-1])


def test_the_order_lands_on_the_numbers_he_dictated() -> None:
    """His own arithmetic, dictated on 04/09: the centre at 100, the botinha at 90, the buy at
    91, and the stop at 81.

    The fixture is built so the two lines come out at exactly those numbers rather than near
    them, because the whole point is to check the arithmetic against the man who wrote it: ten
    percent of a band of ten is one, and the stop is a whole band below the entry.
    """
    order = _order([_HIS_EXAMPLE], ZoneKind.DEMAND)

    assert order is not None
    assert order.side is Side.LONG
    assert order.limit_price == Decimal("91.00")
    assert order.stop_loss == Decimal("81.00")
    assert order.risk == Decimal("10.00")


def test_the_order_is_the_exact_mirror_of_his_numbers_over_a_supply_region() -> None:
    """Reflect his example around 190 and both levels must reflect with it: 190 - 91 = 99 and
    190 - 81 = 109.

    A sell whose entry was still computed *upward* from the botinha would land at 101 with the
    stop at 111 — a trade in the right direction, at prices nobody chose, and no state anywhere
    would be wrong.
    """
    order = _order([_HIS_EXAMPLE_MIRRORED], ZoneKind.SUPPLY)

    assert order is not None
    assert order.side is Side.SHORT
    assert order.limit_price == Decimal("99.00")
    assert order.stop_loss == Decimal("109.00")
    assert order.risk == Decimal("10.00")


def test_the_order_chases_the_botinha_and_the_risk_shrinks_behind_it() -> None:
    """His choice, asked directly and answered: the order chases the botinha.

    Both lines are cumulative, so every close moves them. What that produces is not obvious and is
    worth pinning: the entry creeps **up** while the risk falls, because `d` is a volume-weighted
    mean of `(H + C - 2L)/3` — the *shape* of the bars, not the level of price — and quiet bars
    dilute the big one the anchor sits on. Three bars take the risk from 1.44 to 1.02.
    """
    chase = [
        *_INTO_DEMAND,
        bar(4, open_="97.20", high="97.40", low="96.00", close="96.30", tick_volume=900),
        bar(5, open_="96.30", high="96.50", low="95.60", close="95.80", tick_volume=1100),
    ]
    formation = VwapFormation(_region(ZoneKind.DEMAND))
    trigger = BotinhaTrigger()
    placed: list[tuple[Money, Money, Money]] = []

    with localcontext(ENGINE_CONTEXT):
        for candle in chase:
            formation.update(candle)
            order = trigger.order_for(formation, tick=TICK, candle=candle)
            if order is not None:
                placed.append((order.limit_price, order.stop_loss, order.risk))

    assert placed == [
        (Decimal("95.24"), Decimal("93.80"), Decimal("1.44")),
        (Decimal("95.44"), Decimal("94.22"), Decimal("1.22")),
        (Decimal("95.49"), Decimal("94.47"), Decimal("1.02")),
    ]


def test_the_rounding_widens_the_risk_rather_than_shaving_the_stop() -> None:
    """Both levels are snapped away from the trade, so the placed risk is *wider* than `d`.

    The unrounded arithmetic gives an entry of 95.2322 and a stop of 93.7989 — a distance of
    exactly `d`, 1.43333. On the grid they become 95.24 and 93.80, and the distance becomes 1.44.
    Rounding the other way would have handed the trade a better entry and a nearer stop, which is
    the same lie twice: a fill it might not have got, sized against a risk it is not taking.

    This is also why `risk` is read off the placed levels. Sizing against the unrounded `d` would
    put the wrong amount of money behind every one of these orders, and the error would be
    invisible — the two numbers agree to the second decimal.
    """
    formation = VwapFormation(_region(ZoneKind.DEMAND))
    with localcontext(ENGINE_CONTEXT):
        for candle in _INTO_DEMAND:
            formation.update(candle)
        lines = formation.lines()
        order = BotinhaTrigger().order_for(formation, tick=TICK, candle=_INTO_DEMAND[-1])

    assert lines is not None
    assert order is not None
    unrounded = lines.vwap - lines.botinha
    assert str(unrounded) == "1.43333333333333333333333333"
    assert order.limit_price == Decimal("95.24")  # 95.2322... rounded *up*, against the buyer
    assert order.stop_loss == Decimal("93.80")  # 93.7989... rounded *down*, away from the entry
    assert order.risk == Decimal("1.44")
    assert order.risk > unrounded


def test_the_rounding_is_mirrored_over_a_supply_region() -> None:
    """The sell side rounds the other way, and the fixture is off-grid so it can tell.

    Entry 94.7677 floors to 94.76 and the stop 96.1933 ceils to 96.20 — again a risk of 1.44,
    wider than `d`. Mirrored wrongly the pair would be 94.77 and 96.19: a risk of 1.42, *narrower*
    than the band it was measured from, which is the sell side quietly sizing bigger than the buy
    side on identical geometry.
    """
    order = _order(_INTO_SUPPLY, ZoneKind.SUPPLY)

    assert order is not None
    assert order.limit_price == Decimal("94.76")
    assert order.stop_loss == Decimal("96.20")
    assert order.risk == Decimal("1.44")


def test_an_entry_that_came_out_above_the_close_places_nothing() -> None:
    """A buy limit at or above the market is not a limit — it is a market order wearing one.

    Reachable, and this is the shape: a bar with a huge upper wick that closes near its own low
    drags the botinha up to that low while leaving `d` enormous, so the entry lands above the
    close. Here the lines are 133.37 and 100.00 with the bar closing at 100.10, and the entry
    would be 103.34 — filled instantly at whatever the market offers, which is exactly the trade
    this setup exists to avoid.

    The formation stays alive: it is the level that is unusable on this bar, not the setup.
    """
    spike = bar(1, open_="100.05", high="200", low="100", close="100.10", tick_volume=1000)
    formation = VwapFormation(_region(ZoneKind.DEMAND))
    with localcontext(ENGINE_CONTEXT):
        formation.update(spike)
        order = BotinhaTrigger().order_for(formation, tick=TICK, candle=spike)

    assert order is None
    state = formation.state
    assert state is FormationState.ANCHORED


def test_a_level_that_falls_through_zero_places_nothing() -> None:
    """A band tall against its own price puts a long's stop under nothing.

    Lines at 1.3667 and 0.1000 give an entry of 0.23 and a stop of -1.04. `Signal` refuses a
    non-positive level outright, so the run would end in an exception raised from inside a
    backtest; caught here, the setup simply goes quiet.

    ⚠️ This is the *stop* half of that guard, and it is the only half positive prices can reach:
    an entry is `botinha ± 10%·d` with `d` at most two thirds of the botinha, so it never crosses
    zero while prices are positive. The entry half needs prices that are not — see the test below.
    """
    order = _order(
        [bar(1, open_="0.5", high="3", low="0.1", close="1", tick_volume=1000)],
        ZoneKind.DEMAND,
        top="3",
        bottom="0.1",
    )

    assert order is None


def test_a_formation_with_nothing_to_say_places_nothing() -> None:
    """The three silences of the formation are one silence here, and all three are honest.

    Not anchored, anchored over a series with no volume, and dead: none of them is a level, and
    the trigger has no opinion the formation has not already formed.
    """
    trigger = BotinhaTrigger()
    watching = VwapFormation(_region(ZoneKind.DEMAND))
    with localcontext(ENGINE_CONTEXT):
        assert trigger.order_for(watching, tick=TICK, candle=_INTO_DEMAND[0]) is None

    mute = [
        bar(1, open_="100.50", high="100.60", low="98.00", close="98.50"),
        bar(2, open_="98.50", high="98.80", low="95.00", close="95.50"),
        bar(3, open_="95.50", high="97.50", low="95.20", close="97.20"),
    ]
    assert _order(mute, ZoneKind.DEMAND) is None

    killed = [*_INTO_DEMAND, bar(4, open_="95.00", high="95.10", low="89.00", close="94.00")]
    assert _order(killed, ZoneKind.DEMAND) is None


def test_a_margin_that_is_not_inside_the_band_is_refused() -> None:
    """Zero rests the order on the botinha and one rests it on the centre line — the two ends at
    which the entry stops being an entry and becomes one of the lines it was measured from."""
    for margin in (Decimal(0), Decimal(1), Decimal("-0.1"), Decimal("1.5")):
        with pytest.raises(EngineError, match="strictly between 0 and 1"):
            BotinhaTrigger(margin=margin)


def test_the_margin_moves_the_entry_and_leaves_the_stop_a_whole_band_behind() -> None:
    """A wider margin walks the entry up the band; the stop follows it, a whole `d` behind.

    Pinned because the two are computed from the same `d` and it would be easy to write a version
    where the margin quietly widened the stop as well — leaving the risk fixed while the entry
    moved, which is a different trade from the one he described.
    """
    order = _order([_HIS_EXAMPLE], ZoneKind.DEMAND, trigger=BotinhaTrigger(margin=Decimal("0.5")))

    assert order is not None
    assert order.limit_price == Decimal("95.00")  # halfway up the band of ten, not a tenth
    assert order.stop_loss == Decimal("85.00")  # still a whole band below the entry
    assert order.risk == Decimal("10.00")


def test_two_lines_that_have_met_place_nothing() -> None:
    """A band of zero width is no band, and reaching one takes both halves of this module.

    I first argued this was unreachable: `d` is a volume-weighted mean of `(H + C - 2L)/3`, which
    is zero only for a bar with no range at all, and a bar with no range cannot close up — so the
    confirming bar always contributes something positive. The argument is sound and the conclusion
    was wrong, because it forgot the other rule in this file: **a bar with no volume is skipped**.

    So the reachable shape is a flat bar carrying volume, confirmed by a bar carrying none. The
    confirmation is real — the formation anchors — but the average never sees it, and both lines
    sit on the same price. The entry, the stop and the botinha would be one number, and the risk
    manager would size a trade against a distance of zero.

    Kept and tested rather than argued away, which is the difference between a guard and a
    comment: the reasoning that says a branch cannot be reached is exactly the reasoning that
    stops being true when someone adds a second way in.
    """
    coincident = [
        bar(1, open_="95", high="95", low="95", close="95", tick_volume=1000),
        bar(2, open_="95", high="96", low="95", close="96", tick_volume=0),
    ]
    formation = VwapFormation(_region(ZoneKind.DEMAND))
    with localcontext(ENGINE_CONTEXT):
        for candle in coincident:
            formation.update(candle)
        lines = formation.lines()
        order = BotinhaTrigger().order_for(formation, tick=TICK, candle=coincident[-1])

    # The formation is healthy and has something to say: it anchored, and both lines exist.
    state = formation.state
    assert state is FormationState.ANCHORED
    assert lines is not None
    assert lines.vwap == lines.botinha == Decimal("95")
    # It is the trigger that has nothing to place.
    assert order is None


def test_an_entry_that_lands_exactly_on_the_close_places_nothing() -> None:
    """The boundary of the crossed-limit guard, and nothing else in this file reaches it.

    A buy limit *at* the close is not a limit either: the next bar opens at or around that price
    and takes it immediately, which is the market order the whole entry exists to avoid. The
    fixture is built to land on it exactly — lines at 110 and 100 over a bar closing at 101 put
    the entry at 101.00, to the cent.

    Written because `>=` and `>` are one character apart and every other scenario here is
    comfortably on one side or the other, so both spellings agree everywhere but here.
    """
    lands_on_the_close = bar(
        1, open_="100.50", high="129", low="100", close="101", tick_volume=1000
    )
    formation = VwapFormation(_region(ZoneKind.DEMAND))
    with localcontext(ENGINE_CONTEXT):
        formation.update(lands_on_the_close)
        lines = formation.lines()
        order = BotinhaTrigger().order_for(formation, tick=TICK, candle=lands_on_the_close)

    assert lines is not None
    # The raw arithmetic really does land on the close: 100 + 10% of a band of ten.
    assert lines.vwap == Decimal("110")
    assert lines.botinha == Decimal("100")
    assert order is None


def test_an_entry_through_zero_places_nothing_even_when_the_stop_is_fine() -> None:
    """The entry half of the zero guard, which needs prices that are themselves negative.

    Not a hypothetical: crude went below zero in April 2020, and an engine whose levels are
    Decimal has no opinion about the sign of a price. Over a supply region of [-10, -1] the lines
    come out at -6 and -1, so the entry is -1.50 while the stop is a healthy +3.50 — the one
    shape where checking only the stop lets a non-positive level through, and `Signal` would then
    raise from inside the run rather than the setup going quiet.

    ⚠️ It is also the answer to "is this branch dead?", which is a different question from "can I
    reach it today". With positive prices it is unreachable and provably so. That is an argument
    for testing the case that reaches it, not for deleting the line.
    """
    negative = bar(1, open_="-2", high="-1", low="-9", close="-8", tick_volume=1000)
    formation = VwapFormation(_region(ZoneKind.SUPPLY, top="-1", bottom="-10"))
    with localcontext(ENGINE_CONTEXT):
        formation.update(negative)
        lines = formation.lines()
        order = BotinhaTrigger().order_for(formation, tick=TICK, candle=negative)

    assert lines is not None
    assert lines.vwap == Decimal("-6")
    assert lines.botinha == Decimal("-1")
    assert order is None


def test_an_entry_that_came_out_below_the_close_places_nothing_on_a_sale() -> None:
    """The mirror of the crossed-limit guard, and it is a *different* bug from the pasted branch.

    Pasting the buy branch over the sell one is caught by the mirrored goldens. **Deleting** the
    sell half is not: with `else False` every scenario in this file still passes, because none of
    them makes the guard fire on a short. The two mutants are one line apart and only one had a
    test — the same shape that blocked #189 and #190.

    The fixture is the exact reflection of the buy-side spike: a long wick *down* that closes near
    its own high drags the botinha down to that high while `d` stays enormous, so
    `botinha - 10%·d` lands at 196.66 with the market at 199.90. A sell limit *below* the market
    is marketable — the next open takes it at whatever the venue offers, which is the market
    order this entry exists to avoid, and the backtest would simply look better for it.
    """
    spike = bar(1, open_="199.95", high="200", low="100", close="199.90", tick_volume=1000)
    formation = VwapFormation(_region(ZoneKind.SUPPLY, top="200", bottom="190"))
    with localcontext(ENGINE_CONTEXT):
        formation.update(spike)
        order = BotinhaTrigger().order_for(formation, tick=TICK, candle=spike)

    assert order is None
    state = formation.state
    assert state is FormationState.ANCHORED


def test_an_entry_that_lands_exactly_on_the_close_places_nothing_on_a_sale() -> None:
    """The boundary of that guard on the sell side: `<=` against `<`, one character apart.

    Lines at 90 and 100 over a bar closing at 99 put the entry at exactly 99.00. The buy side has
    had this test since the trigger was written; the sell side had the rule and no scenario that
    reached its edge, which is what "one test per branch" does not buy you.
    """
    lands_on_the_close = bar(1, open_="99.5", high="100", low="71", close="99", tick_volume=1000)
    formation = VwapFormation(_region(ZoneKind.SUPPLY))
    with localcontext(ENGINE_CONTEXT):
        formation.update(lands_on_the_close)
        lines = formation.lines()
        order = BotinhaTrigger().order_for(formation, tick=TICK, candle=lands_on_the_close)

    assert lines is not None
    assert lines.vwap == Decimal("90")
    assert lines.botinha == Decimal("100")
    assert order is None


def test_a_level_that_lands_exactly_on_zero_places_nothing() -> None:
    """`<=` and not `<`, and only a sale can show it.

    On a buy the boundary is subsumed: an entry of exactly zero puts the stop at `-d`, so the
    stop half of the guard fires anyway and the two spellings agree. On a sale the stop is
    `entry + d`, comfortably positive, so a zero entry is the one level at which `<= 0` and `< 0`
    part company — and `OrderRequest` refuses a non-positive limit with a `ValueError`, which is
    the exception from inside a running backtest that this guard exists to prevent.

    Lines at -9 and 1 over a region of [-5, 1] put the entry at 0.00 exactly, with the stop at a
    healthy 10.00. The prices are negative because that is the only way to reach the edge, and
    negative prices are not a thought experiment: crude traded there in April 2020.
    """
    negative = bar(1, open_="-7", high="1", low="-20", close="-8", tick_volume=1000)
    formation = VwapFormation(_region(ZoneKind.SUPPLY, top="1", bottom="-5"))
    with localcontext(ENGINE_CONTEXT):
        formation.update(negative)
        lines = formation.lines()
        order = BotinhaTrigger().order_for(formation, tick=TICK, candle=negative)

    assert lines is not None
    assert lines.vwap == Decimal("-9")
    assert lines.botinha == Decimal("1")
    # The entry is exactly the boundary, and the stop is nowhere near it.
    assert order is None

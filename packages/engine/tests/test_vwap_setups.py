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

from tradeforge_engine.domain import Candle, Side
from tradeforge_engine.errors import EngineError
from tradeforge_engine.loop import ENGINE_CONTEXT
from tradeforge_engine.structure import OrderBlock, StructureKind, ZoneKind
from tradeforge_engine.testing import START, bar
from tradeforge_engine.vwap_setups import FormationState, VwapFormation, VwapLines


def _region(kind: ZoneKind) -> OrderBlock:
    """The author's standing example region, [90, 100], on either side of the book."""
    return OrderBlock(
        kind=kind,
        top=Decimal("100"),
        bottom=Decimal("90"),
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

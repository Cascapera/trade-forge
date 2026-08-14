"""Swing detection, checked against series worked out by hand.

The weight here is the *timing*, not just the arithmetic. A pivot is trivial to spot in
hindsight; the whole point is that the engine may only know it `strength` bars late, and the
`Swing` it returns must be stamped with the bar it happened on, not the bar it was found on. The
goldens pin exactly which update confirms which swing, so a change that let a swing surface early
— the anti-lookahead bug — fails loudly.
"""

from datetime import datetime
from decimal import Decimal
from itertools import pairwise

import pytest
from hypothesis import given
from hypothesis import strategies as st

from tradeforge_engine.domain import Candle
from tradeforge_engine.structure import (
    FairValueGap,
    FVGDetector,
    FVGKind,
    LiquidityDetector,
    LiquidityPool,
    LiquiditySide,
    MarketStructure,
    OrderBlock,
    OrderBlockDetector,
    StructureBreak,
    StructureKind,
    Sweep,
    SweepDetector,
    Swing,
    SwingDetector,
    SwingKind,
    TrackedZone,
    Trend,
    ZoneKind,
    _Region,
    _WedgeTracker,
)
from tradeforge_engine.testing import BULLISH_START, GAPPING_IMPULSE, HOUR, START, bar


def _run(detector: SwingDetector, candles: list[Candle]) -> list[tuple[int, Swing]]:
    """Feed the candles in order, returning (update_index, swing) for every swing that confirmed
    — the index is the bar whose `update` surfaced it, which must lag the swing's own bar."""
    found: list[tuple[int, Swing]] = []
    for index, candle in enumerate(candles):
        for swing in detector.update(candle):
            found.append((index, swing))
    return found


def _at(index: int) -> datetime:
    return START + index * HOUR


# highs 10, 12, 11, 13, 9 · lows 8, 9, 7, 8, 6 — a zig-zag with one clean high, low, high.
_GOLDEN = [
    bar(0, open_="9", close="9", high="10", low="8"),
    bar(1, open_="10", close="11", high="12", low="9"),
    bar(2, open_="9", close="9", high="11", low="7"),
    bar(3, open_="10", close="12", high="13", low="8"),
    bar(4, open_="7", close="8", high="9", low="6"),
]


def test_swings_confirm_one_bar_late_with_strength_one() -> None:
    """strength 1 needs one bar each side, so bar K's swing surfaces on bar K+1.

    - bar 1 (high 12) beats bars 0 and 2 -> a swing high, confirmed on bar 2.
    - bar 2 (low 7) beats bars 1 and 3 -> a swing low, confirmed on bar 3.
    - bar 3 (high 13) beats bars 2 and 4 -> a swing high, confirmed on bar 4.
    Each swing carries the time it *occurred*, one hour before the bar that confirmed it.
    """
    found = _run(SwingDetector(strength=1), _GOLDEN)

    assert found == [
        (2, Swing(kind=SwingKind.HIGH, price=Decimal("12"), time=_at(1))),
        (3, Swing(kind=SwingKind.LOW, price=Decimal("7"), time=_at(2))),
        (4, Swing(kind=SwingKind.HIGH, price=Decimal("13"), time=_at(3))),
    ]


def test_the_confirmed_swing_belongs_to_the_past_not_the_present() -> None:
    """The anti-lookahead property, stated directly: the swing surfaced on bar 2 happened on bar
    1. Trading it on bar 1 would be using a level the market had not yet revealed."""
    confirm_index, swing = _run(SwingDetector(strength=1), _GOLDEN)[0]
    assert confirm_index == 2
    assert swing.time == _at(1)
    assert swing.time < _at(confirm_index)


def test_last_levels_track_the_most_recent_confirmed_swing() -> None:
    detector = SwingDetector(strength=1)
    _run(detector, _GOLDEN)
    assert detector.last_swing_high == Decimal("13")  # bar 3
    assert detector.last_swing_low == Decimal("7")  # bar 2


def test_nothing_confirms_until_the_window_is_full() -> None:
    """strength 2 needs two bars each side — five in all — so the first four updates are silent."""
    detector = SwingDetector(strength=2)
    quiet = [bar(i, open_=str(i), close=str(i), high=str(i + 1), low="0") for i in range(4)]
    assert _run(detector, quiet) == []


def test_equal_highs_are_not_a_swing() -> None:
    """A plateau of equal highs is liquidity, not a pivot: strict `>` means the tie forms none."""
    plateau = [
        bar(0, open_="9", close="9", high="10", low="8"),
        bar(1, open_="11", close="11", high="12", low="9"),
        bar(2, open_="11", close="11", high="12", low="9"),  # equal high with bar 1
        bar(3, open_="9", close="9", high="10", low="8"),
    ]
    highs = [s for _, s in _run(SwingDetector(strength=1), plateau) if s.kind is SwingKind.HIGH]
    assert highs == []


def test_an_outside_bar_is_both_a_high_and_a_low() -> None:
    """One bar can be the highest high and the lowest low of its window — it confirms both."""
    outside = [
        bar(0, open_="9", close="9", high="10", low="8"),
        bar(1, open_="10", close="10", high="15", low="5"),  # engulfs both neighbours
        bar(2, open_="9", close="9", high="10", low="8"),
    ]
    swings = [s.kind for _, s in _run(SwingDetector(strength=1), outside)]
    assert sorted(swings) == [SwingKind.HIGH, SwingKind.LOW]


def test_a_non_positive_strength_is_refused() -> None:
    with pytest.raises(ValueError, match="swing strength must be >= 1"):
        SwingDetector(strength=0)


@given(
    strength=st.integers(min_value=1, max_value=4),
    bars=st.lists(
        st.tuples(st.integers(min_value=0, max_value=50), st.integers(min_value=0, max_value=20)),
        min_size=0,
        max_size=40,
    ),
)
def test_every_swing_lags_by_exactly_strength_and_is_the_window_extreme(
    strength: int, bars: list[tuple[int, int]]
) -> None:
    """Two invariants over random data: a swing surfaces exactly `strength` bars after it occurs,
    and its price is the strict extreme of its `2*strength+1` window. `low` is the base and `span`
    lifts the high, so every candle is valid (high >= low)."""
    candles = [
        bar(index, open_=str(low), close=str(low), high=str(low + span), low=str(low))
        for index, (low, span) in enumerate(bars)
    ]
    for confirm_index, swing in _run(SwingDetector(strength=strength), candles):
        origin = confirm_index - strength
        assert swing.time == candles[origin].time  # lagged by exactly `strength`

        window = candles[confirm_index - 2 * strength : confirm_index + 1]
        others = [c for c in window if c.time != swing.time]
        if swing.kind is SwingKind.HIGH:
            assert all(swing.price > other.high for other in others)
        else:
            assert all(swing.price < other.low for other in others)


@given(
    strength=st.integers(min_value=1, max_value=4),
    bars=st.lists(
        st.tuples(st.integers(min_value=0, max_value=50), st.integers(min_value=0, max_value=20)),
        min_size=0,
        max_size=40,
    ),
)
def test_reports_exactly_the_strict_window_extremes(
    strength: int, bars: list[tuple[int, int]]
) -> None:
    """Completeness, not just soundness: the detector reports *every* strict window extreme and no
    others. Brute-force each interior bar independently of the sliding deque, so an off-by-one or
    a skipped bar in the incremental path would make the two sets disagree."""
    candles = [
        bar(index, open_=str(low), close=str(low), high=str(low + span), low=str(low))
        for index, (low, span) in enumerate(bars)
    ]
    detected = {(s.time, s.kind) for _, s in _run(SwingDetector(strength=strength), candles)}

    expected: set[tuple[datetime, SwingKind]] = set()
    for i in range(strength, len(candles) - strength):
        others = candles[i - strength : i] + candles[i + 1 : i + strength + 1]
        if all(candles[i].high > o.high for o in others):
            expected.add((candles[i].time, SwingKind.HIGH))
        if all(candles[i].low < o.low for o in others):
            expected.add((candles[i].time, SwingKind.LOW))

    assert detected == expected


# --------------------------------------------------------------------------- #
# Fair value gaps — the three-candle imbalance                                  #
# --------------------------------------------------------------------------- #


def _fvgs(candles: list[Candle]) -> list[FairValueGap | None]:
    detector = FVGDetector()
    return [detector.update(candle) for candle in candles]


def test_a_bullish_fvg_is_the_untraded_band_below_a_leap_up() -> None:
    """c1.high 10 < c3.low 12: the middle bar leapt up, leaving 10-12 untraded. It takes three
    candles, so the gap surfaces on c3, and the zone is [bottom 10, top 12]."""
    bullish = [
        bar(0, open_="9", close="9", high="10", low="8"),
        bar(1, open_="12", close="14", high="15", low="11"),  # impulse up
        bar(2, open_="13", close="15", high="16", low="12"),
    ]
    results = _fvgs(bullish)
    assert results[:2] == [None, None]
    assert results[2] == FairValueGap(
        kind=FVGKind.BULLISH, top=Decimal("12"), bottom=Decimal("10"), time=_at(2)
    )


def test_a_bearish_fvg_is_the_untraded_band_above_a_leap_down() -> None:
    """The mirror: c1.low 14 > c3.high 12, an untraded band from 12 up to 14."""
    bearish = [
        bar(0, open_="15", close="15", high="16", low="14"),
        bar(1, open_="12", close="10", high="13", low="9"),  # impulse down
        bar(2, open_="11", close="9", high="12", low="8"),
    ]
    assert _fvgs(bearish)[2] == FairValueGap(
        kind=FVGKind.BEARISH, top=Decimal("14"), bottom=Decimal("12"), time=_at(2)
    )


def test_overlapping_candles_leave_no_gap() -> None:
    candles = [bar(i, open_="10", close="10", high="11", low="9") for i in range(3)]
    assert _fvgs(candles) == [None, None, None]


def test_a_touch_is_not_a_gap() -> None:
    """Strict inequality: c1.high 12 exactly meeting c3.low 12 is a touch, not an untraded band."""
    touch = [
        bar(0, open_="11", close="11", high="12", low="10"),
        bar(1, open_="13", close="15", high="16", low="12"),
        bar(2, open_="14", close="15", high="17", low="12"),  # c3.low 12 == c1.high 12
    ]
    assert _fvgs(touch)[2] is None


def test_a_bearish_touch_is_not_a_gap() -> None:
    """The mirror of the bullish touch: c1.low 12 exactly meeting c3.high 12 is no gap either."""
    touch = [
        bar(0, open_="13", close="13", high="14", low="12"),
        bar(1, open_="11", close="9", high="12", low="8"),
        bar(2, open_="10", close="9", high="12", low="7"),  # c3.high 12 == c1.low 12
    ]
    assert _fvgs(touch)[2] is None


def test_the_gap_spans_the_last_three_candles_not_the_first_ever() -> None:
    """The window slides: bars 0-1-2 overlap, but 1-2-3 leap, so the gap is between bar 1 and bar 3.
    Its first candle is the window's first, not the run's: the detector uses the last three."""
    series = [
        bar(0, open_="13", close="13", high="14", low="12"),
        bar(1, open_="12", close="12", high="13", low="11"),  # c1 of the gap
        bar(2, open_="14", close="16", high="17", low="13"),  # impulse
        bar(3, open_="15", close="17", high="18", low="14"),  # c3.low 14 > c1.high 13
    ]
    results = _fvgs(series)
    assert results[:3] == [None, None, None]
    assert results[3] == FairValueGap(
        kind=FVGKind.BULLISH, top=Decimal("14"), bottom=Decimal("13"), time=_at(3)
    )


@given(
    bars=st.lists(
        st.tuples(st.integers(min_value=0, max_value=100), st.integers(min_value=0, max_value=20)),
        min_size=0,
        max_size=40,
    )
)
def test_the_detector_reports_exactly_the_strict_three_candle_gaps(
    bars: list[tuple[int, int]],
) -> None:
    """The full biconditional. A reported gap is a strict inefficiency between the first and third
    candle of its window **whose middle candle closed clear of it**, zoned exactly by their wicks
    and timed to the third bar (soundness); and where it reports nothing, one of those did not
    hold (completeness) — so a flipped bound or a non-strict compare fails one half or the other.

    The middle candle's close is part of the rule, not a stray filter: it is his indicator's
    `close[1] > high[2]`, and an earlier version of this property asserted its *absence*. These
    fixtures set open == close == low, so for a bullish gap the test reads as the middle bar's low
    clearing the first bar's high."""
    candles = [
        bar(index, open_=str(low), close=str(low), high=str(low + span), low=str(low))
        for index, (low, span) in enumerate(bars)
    ]
    detector = FVGDetector()
    for index, candle in enumerate(candles):
        gap = detector.update(candle)
        if index < 2:
            assert gap is None  # no full window yet
            continue
        first, third = candles[index - 2], candle
        middle = candles[index - 1]
        if gap is None:
            # completeness: a silent bar must hide no gap the author would have marked.
            assert not (first.high < third.low and middle.close > first.high)
            assert not (first.low > third.high and middle.close < first.low)
            continue
        assert gap.time == third.time
        assert gap.top > gap.bottom
        if gap.kind is FVGKind.BULLISH:
            assert first.high < third.low
            assert middle.close > first.high  # the author's own condition
            assert gap.bottom == first.high
            assert gap.top == third.low
        else:
            assert first.low > third.high
            assert middle.close < first.low  # his condition, on the side that had none
            assert gap.top == first.low
            assert gap.bottom == third.high


# --------------------------------------------------------------------------- #
# Market structure — BOS and CHoCH                                              #
# --------------------------------------------------------------------------- #


def _index_of(candle: Candle) -> int:
    """The bar number `bar()` stamped into this candle — negative for anything before bar 0.

    Read from the candle rather than from `enumerate`, so that a scenario prefixed with the
    bootstrap below still reports its own bars by their own numbers.
    """
    return round((candle.time - START) / HOUR)


def _breaks(candles: list[Candle]) -> list[tuple[int, StructureBreak]]:
    structure = MarketStructure()
    found: list[tuple[int, StructureBreak]] = []
    for candle in candles:
        event = structure.update(candle)
        if event is not None:
            found.append((_index_of(candle), event))
    return found


def _bos(  # noqa: PLR0913 — keyword-only; a break simply has this many facts
    trend: Trend, level: str, at: int, *, level_at: int, origin: str, origin_at: int
) -> StructureBreak:
    """A bullish/bearish BOS confirmed on bar `at`, whose impulse started at `origin` on
    `origin_at` — the extreme the move came from, which is also the next opposite CHoCH anchor.

    `level_at` is the bar that *set* the broken level, and is required rather than defaulted: a
    default would let a new golden be written without stating it, and quietly assert whatever
    that default happened to be against whatever the engine produced."""
    return StructureBreak(
        kind=StructureKind.BOS,
        trend=trend,
        level=Decimal(level),
        time=_at(at),
        level_time=_at(level_at),
        origin=Decimal(origin),
        origin_time=_at(origin_at),
    )


def _choch(  # noqa: PLR0913 — see _bos
    trend: Trend, level: str, at: int, *, level_at: int, origin: str, origin_at: int
) -> StructureBreak:
    """A CHoCH confirmed on bar `at`; `origin` is the extreme the reversing move began from."""
    return StructureBreak(
        kind=StructureKind.CHOCH,
        trend=trend,
        level=Decimal(level),
        time=_at(at),
        level_time=_at(level_at),
        origin=Decimal(origin),
        origin_time=_at(origin_at),
    )


def _breaks_from_bullish(candles: list[Candle]) -> list[tuple[int, StructureBreak]]:
    """`_breaks`, on a machine already in an uptrend, reporting only what `candles` produced.

    The prefix's own two events are dropped rather than repeated in every golden that uses it:
    they are not what those scenarios are about, and the test above pins them once.
    """
    return [(index, event) for index, event in _breaks([*BULLISH_START, *candles]) if index >= 0]


def test_the_bullish_start_is_a_bearish_bos_then_a_bullish_choch() -> None:
    """What the shared prefix does, stated once so the scenarios using it need not restate it.

    It is also the shape of every fresh series, which is worth pinning for its own sake: the
    machine begins at the indicator's `DIR = -1`, so the first thing it can mark is a bearish BOS,
    and it takes a change of character to turn the bias up.
    """
    assert _breaks(BULLISH_START) == [
        (-2, _bos(Trend.BEARISH, "88", -2, level_at=-6, origin="92", origin_at=-4)),
        (-1, _choch(Trend.BULLISH, "92", -1, level_at=-4, origin="86", origin_at=-2)),
    ]


def test_the_bullish_start_hands_over_a_state_that_can_decide_nothing() -> None:
    """The prefix's licence, asserted instead of asserted-in-a-docstring.

    Every scenario in this file reads as a statement about its own bars, and that is only true if
    what the prefix leaves behind cannot produce an event of its own. Three residues could, and
    each is closed differently:

    * **`_armed_high` is `None`.** The CHoCH cleared it, so no bullish break is half-way armed.
    * **The bearish anchor is 86**, beneath every scenario here — and where a scenario does dig
      that deep, its own first break has replanted the anchor long before it gets there.
    * **The running high is 94**, and this one is closed structurally rather than by level:
      arming reads `falling`, which needs `previous.high < before.high`. For bar 0 of any
      scenario that is bar -1's 94 against bar -2's 90, which is false. There is no sequence of
      scenario bars that arms a bullish BOS at 94 — not "it is out of reach", but "that path does
      not exist".

    Reaching into the private state is the point: these are exactly the values no public result
    exposes, and a change to the prefix that moved them would otherwise be found as an unrelated
    golden failing somewhere else entirely.

    ⚠️ **The last assertion is a hand-copy of a rule that lives elsewhere.** It restates, over two
    literals, the second clause of `falling` in `MarketStructure.update` — and it is the *only*
    thing tying the two together. It can therefore never fail because of a change to the engine:
    if `falling` is ever given a different criterion, this line goes on passing while defending a
    property it no longer establishes. It is a statement about the fixture, not a guard on the
    code, and it has to be re-read by hand if that comparison moves.
    """
    structure = MarketStructure()
    for candle in BULLISH_START:
        structure.update(candle)

    assert structure._armed_high is None
    assert structure._choch_down == Decimal("86")
    assert structure._high_up == Decimal("94")
    # See the warning above: this mirrors `falling`'s `previous.high < before.high` by hand.
    assert not BULLISH_START[-1].high < BULLISH_START[-2].high


# The author's hand-worked example, unchanged: a bullish BOS on bar 5, then a bearish CHoCH on bar
# 8 that turns it. high, low, close per bar (open = close for simplicity).
_STRUCTURE_GOLDEN = [
    bar(0, open_="99", close="99", high="100", low="95"),
    bar(1, open_="104", close="104", high="105", low="99"),  # top = 105
    bar(2, open_="99", close="99", high="103", low="98"),  # correction 1
    bar(3, open_="97", close="97", high="101", low="96"),  # correction 2 -> armed
    bar(4, open_="103", close="103", high="104", low="100"),  # bounce, no break
    bar(5, open_="106", close="106", high="107", low="103"),  # close 106 > 105 -> BOS up
    bar(6, open_="101", close="101", high="105", low="100"),  # correction
    bar(7, open_="99", close="99", high="103", low="98"),  # correction
    bar(8, open_="95", close="95", high="100", low="94"),  # close 95 < 96 -> CHoCH down
]


def test_structure_matches_the_hand_worked_example() -> None:
    """A bullish BOS on bar 5 (close 106 above the 105 top, after two correction bars and a
    bounce), then a bearish CHoCH on bar 8 (close 95 below 96 — the lowest low the up-move
    defended). Exactly the two events the method's author marked.

    Worth saying plainly, because it is the load-bearing fact of the transcription: these are the
    numbers this golden has always asserted. Porting the indicator did not move a single one of
    them. What the port changed is that the machine now has to be *in* an uptrend to read an
    uptrend's structure, which is what `BULLISH_START` supplies.
    """
    assert _breaks_from_bullish(_STRUCTURE_GOLDEN) == [
        (5, _bos(Trend.BULLISH, "105", 5, level_at=1, origin="96", origin_at=3)),
        (8, _choch(Trend.BEARISH, "96", 8, level_at=3, origin="107", origin_at=5)),
    ]


def test_trend_is_none_until_the_first_choch() -> None:
    """A bias has to be *earned*, and only a change of character earns one.

    The indicator's `DIR` starts at -1, so a fresh series already leans bearish — but nothing has
    happened yet to say so, and the bearish BOS on bar -2 merely confirms what was assumed.
    Reporting `BEARISH` there would dress an untested default up as a reading of the market, and
    a strategy gating on `trend is not None` would act on it. The bias becomes a fact on bar -1,
    where the CHoCH turns it.
    """
    structure = MarketStructure()
    for candle in BULLISH_START[:-1]:  # everything up to and including the bearish BOS
        structure.update(candle)
        assert structure.trend is None
    structure.update(BULLISH_START[-1])
    assert structure.trend is Trend.BULLISH

    # And a BOS in the trend's own direction leaves it alone: the golden's bullish BOS on bar 5
    # continues the bias the CHoCH settled rather than re-deciding it.
    for candle in _STRUCTURE_GOLDEN[:6]:
        structure.update(candle)
    assert structure.trend is Trend.BULLISH


def test_the_bearish_mirror_bootstraps_down_then_chochs_up() -> None:
    """The symmetric case: a bearish BOS on bar 5 (close 89 below the 90 bottom, after two up
    correction bars), then a bullish CHoCH on bar 8 (close 104 above 103, the high the down-move
    defended)."""
    mirror = [
        bar(0, open_="96", close="96", high="100", low="95"),
        bar(1, open_="91", close="91", high="99", low="90"),  # bottom = 90
        bar(2, open_="100", close="100", high="101", low="92"),  # up-correction 1
        bar(3, open_="102", close="102", high="103", low="94"),  # up-correction 2 -> armed
        bar(4, open_="95", close="95", high="100", low="93"),  # bounce, no break
        bar(5, open_="89", close="89", high="97", low="88"),  # close 89 < 90 -> BOS down
        bar(6, open_="94", close="94", high="95", low="92"),  # correction
        bar(7, open_="98", close="98", high="99", low="95"),  # correction
        bar(8, open_="104", close="104", high="105", low="100"),  # close 104 > 103 -> CHoCH up
    ]
    assert _breaks(mirror) == [
        (5, _bos(Trend.BEARISH, "90", 5, level_at=1, origin="103", origin_at=3)),
        (8, _choch(Trend.BULLISH, "103", 8, level_at=3, origin="88", origin_at=5)),
    ]


def test_one_correction_bar_does_not_arm_a_bos() -> None:
    """Two consecutive correction bars are required. With only one, the top is unarmed and a close
    above it is not a break of structure.

    The bullish start is what gives this test teeth. Run on a fresh machine it would also report
    nothing — but for the wrong reason, because a machine at `DIR = -1` can mark no bullish break
    whatever these bars do. Then the assertion would hold with the arming rule deleted outright.
    """
    candles = [
        bar(0, open_="99", close="99", high="100", low="95"),
        bar(1, open_="104", close="104", high="105", low="99"),  # top 105
        bar(2, open_="99", close="99", high="103", low="98"),  # a single correction bar
        bar(3, open_="106", close="106", high="107", low="102"),  # closes above 105 but unarmed
    ]
    assert _breaks_from_bullish(candles) == []


def test_a_wick_through_the_top_without_a_close_is_no_bos() -> None:
    """The break is by close, not by pierce: a bar whose high tags the top but whose close stays
    below it does not confirm a BOS (here it simply lifts the top).

    On the bullish start, so that the silence means the *close* rule held — see the note on
    `test_one_correction_bar_does_not_arm_a_bos`.
    """
    candles = [
        bar(0, open_="99", close="99", high="100", low="95"),
        bar(1, open_="104", close="104", high="105", low="99"),  # top 105
        bar(2, open_="99", close="99", high="103", low="98"),  # correction 1
        bar(3, open_="97", close="97", high="101", low="96"),  # correction 2 -> armed
        bar(4, open_="103", close="104", high="107", low="100"),  # high 107 > 105, close 104 < 105
    ]
    assert _breaks_from_bullish(candles) == []


def test_a_non_correction_bar_becomes_the_next_correction_reference() -> None:
    """The author's rule, pinned: after the top, a bar that is not a correction becomes the
    reference, and correction is measured against it, and so on. Bar 2 has a higher low than the
    105 top (not a correction), so it is the reference; bars 3 and 4 step down from it and arm the
    top — a BOS — even though neither dipped below the top's own low of 99.

    The origin is the one number in this file the transcription moved, and moving it is the point
    of the whole port. It is now **99.2 on bar 4** — the deepest the pullback itself went — where
    the rule this replaced said 99 on bar 1, the low of the bar that made the high. Bar 1's low was
    never part of the counter-move the break resolved; it belongs to the leg before it. And the
    origin is load-bearing twice over: it is the anchor the next opposite CHoCH will sit on, and
    the start of the stretch an order block is hunted in. Taking it from a bar the pullback never
    visited is exactly the error he found by drawing on the chart — the level lands too low, and
    the zone and the entry follow it down.
    """
    candles = [
        bar(0, open_="99", close="99", high="100", low="95"),
        bar(1, open_="104", close="104", high="105", low="99"),  # top 105, low 99
        bar(2, open_="103", close="103", high="104", low="100"),  # low 100 > 99: not a correction
        bar(
            3, open_="99.8", close="99.8", high="103", low="99.5"
        ),  # steps down from bar 2 -> corr 1
        bar(
            4, open_="99.5", close="99.5", high="102", low="99.2"
        ),  # steps down from bar 3 -> corr 2
        bar(5, open_="106", close="106", high="107", low="103"),  # close 106 > 105 -> BOS
    ]
    assert _breaks_from_bullish(candles) == [
        (5, _bos(Trend.BULLISH, "105", 5, level_at=1, origin="99.2", origin_at=4)),
    ]


def test_the_anchor_stops_at_the_bar_before_the_one_that_confirmed() -> None:
    """The anchor is the lowest low *up to* the confirming bar, never including it.

    This is the discriminating case for the rule the whole port exists to get right, and no other
    scenario in this file reaches it. `_lowest_since_armed` is advanced **after** the break is
    emitted, while `_low_up` has already absorbed the current bar in `_advance_extremes` — so the
    two agree on every ordinary pullback and part company on exactly one shape: a bar that both
    digs deeper than anything before it *and* closes through the armed top. A V: news knocks price
    to 90, buyers take it back, and it closes at 106 above the 105 top.

    Reading the confirming bar's own low would be a small, plausible transcription slip — `Fundo_
    Sobe` is a real variable in the Pascal and sits one line away — and it damages two things at
    once, because `origin` is used for two:

    * it is the start of the impulse leg the `OrderBlockDetector` hunts gaps in. Moved to 90 on
      bar 4, the leg collapses to the breaking bar alone and the zone the setup would have bought
      is never marked at all;
    * it is the anchor the next bearish CHoCH sits on. Moved to 90, bar 5's close of 94 — the real
      change of character — passes unnoticed, and the reversal is recognised six points late.

    So both halves are asserted: where the anchor is, and that it is the level a later close is
    actually judged against.
    """
    candles = [
        bar(0, open_="99", close="99", high="100", low="95"),
        bar(1, open_="104", close="104", high="105", low="99"),  # top 105
        bar(2, open_="102", close="99", high="103", low="98"),  # correction 1
        bar(3, open_="99", close="97", high="101", low="96"),  # correction 2 -> arms 105, low 96
        bar(4, open_="97", close="106", high="107", low="90"),  # the V: digs to 90, closes 106
        bar(5, open_="106", close="94", high="106", low="93"),  # 94 is under 96 but over 90
    ]
    assert _breaks_from_bullish(candles) == [
        # 96 on bar 3 — the pullback's own low — and not 90 on bar 4, the breaking bar's.
        (4, _bos(Trend.BULLISH, "105", 4, level_at=1, origin="96", origin_at=3)),
        # And 96 is what bar 5 is judged against. Anchored at 90 this bar would be silent.
        (5, _choch(Trend.BEARISH, "96", 5, level_at=3, origin="107", origin_at=4)),
    ]


def test_a_close_exactly_at_the_top_is_not_a_break() -> None:
    """Strict inequality for structure too: a close landing exactly on the top does not confirm.

    On the bullish start, so that the silence means the *strictness* held — see the note on
    `test_one_correction_bar_does_not_arm_a_bos`.
    """
    candles = [
        bar(0, open_="99", close="99", high="100", low="95"),
        bar(1, open_="104", close="104", high="105", low="99"),  # top 105
        bar(2, open_="99", close="99", high="103", low="98"),  # correction 1
        bar(3, open_="97", close="97", high="101", low="96"),  # correction 2 -> armed
        bar(4, open_="104", close="105", high="105", low="100"),  # close 105 == top, not above
    ]
    assert _breaks_from_bullish(candles) == []


def test_a_second_bos_raises_the_choch_anchor() -> None:
    """Continuation, the core of the method: a second bullish BOS re-anchors the CHoCH higher (96
    -> 99), so a later close below 99 but above the old 96 is a CHoCH against the *new* anchor. If
    the anchor had not moved, bar 10 would be no reversal at all."""
    candles = [
        bar(0, open_="99", close="99", high="100", low="95"),
        bar(1, open_="104", close="104", high="105", low="99"),
        bar(2, open_="99", close="99", high="103", low="98"),
        bar(3, open_="97", close="97", high="101", low="96"),
        bar(4, open_="103", close="103", high="104", low="100"),
        bar(5, open_="106", close="106", high="107", low="103"),  # BOS #1: top->107, anchor 96
        bar(6, open_="101", close="101", high="105", low="100"),  # correction 1
        bar(7, open_="100", close="100", high="103", low="99"),  # correction 2 -> armed, low 99
        bar(
            8, open_="108", close="108", high="109", low="104"
        ),  # close 108 > 107 -> BOS #2: anchor->99
        bar(9, open_="102", close="102", high="106", low="101"),  # correction
        bar(
            10, open_="98", close="98", high="103", low="97"
        ),  # close 98 < 99 -> CHoCH at the new anchor
    ]
    assert _breaks_from_bullish(candles) == [
        (5, _bos(Trend.BULLISH, "105", 5, level_at=1, origin="96", origin_at=3)),
        (8, _bos(Trend.BULLISH, "107", 8, level_at=5, origin="99", origin_at=7)),
        (10, _choch(Trend.BEARISH, "99", 10, level_at=7, origin="109", origin_at=8)),
    ]


def test_a_close_exactly_on_the_bottom_is_not_a_bearish_break() -> None:
    """The mirror of `test_a_close_exactly_at_the_top_is_not_a_break`, and it needs no prefix.

    A fresh machine already sits at the indicator's `DIR = -1`, so this is the one direction that
    reads straight from bar 0 — which is also why it went untested: nothing about the scenario
    looked broken, and the sell side simply had no strictness case of its own.

    A close landing exactly on the armed bottom is the everyday shape at a round number, which is
    precisely where the stops that would be swept are stacked. Accepting it opens a short, marks
    supply zones and plants a bullish anchor on a leg that never happened.
    """
    candles = [
        bar(0, open_="101", close="101", high="105", low="100"),
        bar(1, open_="100", close="96", high="101", low="95"),  # bottom 95
        bar(2, open_="97", close="102", high="103", low="97"),  # up-correction 1
        bar(3, open_="102", close="104", high="105", low="99"),  # up-correction 2 -> arms 95
        bar(4, open_="98", close="95", high="98", low="94"),  # close 95 == bottom, not below
    ]
    assert _breaks(candles) == []


def test_a_close_exactly_on_a_choch_anchor_does_not_turn_the_bias() -> None:
    """Strictness on the anchors too, in both directions — the two comparisons that decide a
    *reversal*, and therefore the two whose failure costs most.

    A CHoCH is the only event that changes the bias, so a comparison one tick too generous here
    does not merely add a mark: it hands the next stretch of chart to the wrong trend, and every
    setup gated on `trend` follows it. Both are checked against a golden that is one bar short of
    the real thing, so the anchor is genuinely planted and genuinely tested.
    """
    # Bullish BOS on bar 5 plants the bearish anchor at 96; bar 8 lands exactly on it.
    on_the_down_anchor = [
        *_STRUCTURE_GOLDEN[:6],
        bar(6, open_="101", close="101", high="105", low="100"),
        bar(7, open_="99", close="99", high="103", low="98"),
        bar(8, open_="97", close="96", high="100", low="94"),  # close 96 == anchor, not below
    ]
    assert _breaks_from_bullish(on_the_down_anchor) == [
        (5, _bos(Trend.BULLISH, "105", 5, level_at=1, origin="96", origin_at=3))
    ]

    # The mirror, on a fresh machine: a bearish BOS plants the bullish anchor at 103.
    on_the_up_anchor = [
        bar(0, open_="96", close="96", high="100", low="95"),
        bar(1, open_="91", close="91", high="99", low="90"),  # bottom 90
        bar(2, open_="100", close="100", high="101", low="92"),  # up-correction 1
        bar(3, open_="102", close="102", high="103", low="94"),  # up-correction 2 -> armed
        bar(4, open_="95", close="95", high="100", low="93"),
        bar(5, open_="89", close="89", high="97", low="88"),  # BOS down -> anchor 103
        bar(6, open_="94", close="94", high="95", low="92"),
        bar(7, open_="98", close="98", high="99", low="95"),
        bar(8, open_="100", close="103", high="104", low="100"),  # close 103 == anchor, not above
    ]
    assert _breaks(on_the_up_anchor) == [
        (5, _bos(Trend.BEARISH, "90", 5, level_at=1, origin="103", origin_at=3))
    ]


def test_a_choch_can_flip_back() -> None:
    """After the bearish CHoCH the bias is bearish and the next CHoCH points up at the high the
    failed up-move made (107). A close back above 107 flips it bullish again."""
    extended = [
        *_STRUCTURE_GOLDEN,
        bar(9, open_="96", close="96", high="99", low="93"),  # bearish leg makes a new low
        bar(10, open_="108", close="108", high="109", low="100"),  # close 108 > 107 -> CHoCH up
    ]
    assert _breaks_from_bullish(extended)[-1] == (
        10,
        _choch(Trend.BULLISH, "107", 10, level_at=5, origin="93", origin_at=9),
    )


def test_one_correction_bar_does_not_arm_a_bearish_bos() -> None:
    """The bearish mirror: one up-correction bar does not arm the bottom for a break."""
    candles = [
        bar(0, open_="96", close="96", high="100", low="95"),
        bar(1, open_="91", close="91", high="99", low="90"),  # bottom 90
        bar(2, open_="100", close="100", high="101", low="92"),  # a single up-correction bar
        bar(3, open_="89", close="89", high="94", low="88"),  # closes below 90 but unarmed
    ]
    assert _breaks(candles) == []


def test_a_wick_through_the_bottom_without_a_close_is_no_bearish_bos() -> None:
    """The bearish mirror: a low piercing the bottom while the close holds above it is no BOS."""
    candles = [
        bar(0, open_="96", close="96", high="100", low="95"),
        bar(1, open_="91", close="91", high="99", low="90"),  # bottom 90
        bar(2, open_="100", close="100", high="101", low="92"),  # up-correction 1
        bar(3, open_="102", close="102", high="103", low="94"),  # up-correction 2 -> armed
        bar(4, open_="97", close="96", high="100", low="88"),  # low 88 < 90, close 96 > 90
    ]
    assert _breaks(candles) == []


# --- liquidity pools: equal swings that stack the stops a sweep will hunt -------------------- #


def _swing(kind: SwingKind, price: str, index: int) -> Swing:
    """A confirmed swing at `price`, stamped with the time of bar `index`."""
    return Swing(kind=kind, price=Decimal(price), time=_at(index))


def _liquidity(
    detector: LiquidityDetector, items: list[tuple[Swing, int]]
) -> list[tuple[int, LiquidityPool]]:
    """Feed (swing, bar) pairs in order; return (bar, pool) for every update that formed a pool."""
    found: list[tuple[int, LiquidityPool]] = []
    for swing, bar_index in items:
        pool = detector.update(swing, bar_index)
        if pool is not None:
            found.append((bar_index, pool))
    return found


def test_liquidity_golden_two_equal_highs_form_a_buy_side_pool() -> None:
    """Author's example: highs 100 and 103 within a 3-point tolerance are one pool at the extreme.

    The lows 90, 80, 0 are all more than 3 points apart, so no sell-side pool forms; only the two
    equal highs stack liquidity, and the pool's level is 103 — the line a later sweep must clear.
    """
    detector = LiquidityDetector(tolerance=Decimal("3"))
    pools = _liquidity(
        detector,
        [
            (_swing(SwingKind.HIGH, "100", 0), 0),
            (_swing(SwingKind.LOW, "90", 1), 1),
            (_swing(SwingKind.HIGH, "103", 2), 2),
            (_swing(SwingKind.LOW, "80", 3), 3),
            (_swing(SwingKind.LOW, "0", 4), 4),
        ],
    )
    assert pools == [
        (
            2,
            LiquidityPool(
                side=LiquiditySide.BUY_SIDE,
                level=Decimal("103"),
                touches=(
                    _swing(SwingKind.HIGH, "100", 0),
                    _swing(SwingKind.HIGH, "103", 2),
                ),
                time=_at(2),
            ),
        )
    ]


def test_a_third_touch_deepens_the_pool_and_raises_the_extreme() -> None:
    """A third high within tolerance of the anchor adds a touch and lifts the level to that high."""
    detector = LiquidityDetector(tolerance=Decimal("3"))
    assert detector.update(_swing(SwingKind.HIGH, "100", 0), 0) is None  # anchor 100
    first = detector.update(_swing(SwingKind.HIGH, "102", 2), 2)  # |102-100|=2 -> pool at 102
    second = detector.update(_swing(SwingKind.HIGH, "103", 4), 4)  # |103-100|=3 -> extend to 103

    assert first is not None
    assert first.level == Decimal("102")
    assert len(first.touches) == 2
    assert second is not None
    assert second.level == Decimal("103")  # extreme rises with the deepest touch
    assert len(second.touches) == 3


def test_the_pool_is_anchored_to_its_first_touch_not_the_drifting_extreme() -> None:
    """A staircase of higher highs must not chain into one pool.

    Each touch is measured against the *first* (the anchor), not the running extreme, so once price
    walks beyond tolerance from that anchor it starts a fresh level instead of deepening the old one
    — the author's rule that a trend of higher highs is not a stack of equal highs.
    """
    detector = LiquidityDetector(tolerance=Decimal("3"))
    assert detector.update(_swing(SwingKind.HIGH, "100", 0), 0) is None  # anchor 100
    pool = detector.update(_swing(SwingKind.HIGH, "103", 1), 1)  # |103-100|=3 -> pool
    assert pool is not None
    assert pool.level == Decimal("103")
    assert len(pool.touches) == 2
    # 106 is 6 from the anchor 100, beyond tolerance, so it opens a new lone level, not a 3rd touch.
    assert detector.update(_swing(SwingKind.HIGH, "106", 2), 2) is None


def test_equal_lows_form_a_sell_side_pool_at_the_minimum() -> None:
    """The mirror: two lows within tolerance stack sell-side liquidity at the lower extreme."""
    detector = LiquidityDetector(tolerance=Decimal("2"))
    assert detector.update(_swing(SwingKind.LOW, "90", 0), 0) is None
    pool = detector.update(_swing(SwingKind.LOW, "88", 1), 1)  # |88-90|=2 -> pool

    assert pool is not None
    assert pool.side is LiquiditySide.SELL_SIDE
    assert pool.level == Decimal("88")  # the extreme is the lower low


def test_a_high_beyond_tolerance_starts_its_own_level() -> None:
    """A high further than tolerance from any level is a lone candidate, not a pool."""
    detector = LiquidityDetector(tolerance=Decimal("3"))
    assert detector.update(_swing(SwingKind.HIGH, "100", 0), 0) is None
    assert detector.update(_swing(SwingKind.HIGH, "104", 1), 1) is None  # |104-100|=4 > 3


def test_a_pool_goes_stale_after_the_lookback_window() -> None:
    """A level with no touch inside `lookback_bars` ages out and pairs with nothing."""
    stale = LiquidityDetector(tolerance=Decimal("3"), lookback_bars=200)
    assert stale.update(_swing(SwingKind.HIGH, "100", 0), 0) is None
    # bar 201 is 201 bars on: the first high has expired, so an equal high finds no partner.
    assert stale.update(_swing(SwingKind.HIGH, "101", 201), 201) is None

    fresh = LiquidityDetector(tolerance=Decimal("3"), lookback_bars=200)
    fresh.update(_swing(SwingKind.HIGH, "100", 0), 0)
    # exactly 200 bars later is still inside the window (200 - 0 == 200), so the pool forms.
    assert fresh.update(_swing(SwingKind.HIGH, "101", 200), 200) is not None


def test_min_touches_three_needs_a_third_touch() -> None:
    """Raising `min_touches` withholds the pool until enough swings have stacked."""
    detector = LiquidityDetector(tolerance=Decimal("3"), min_touches=3)
    assert detector.update(_swing(SwingKind.HIGH, "100", 0), 0) is None
    assert detector.update(_swing(SwingKind.HIGH, "101", 1), 1) is None  # 2 touches < 3
    pool = detector.update(_swing(SwingKind.HIGH, "102", 2), 2)  # 3rd touch

    assert pool is not None
    assert len(pool.touches) == 3
    assert pool.level == Decimal("102")


def test_the_nearest_level_wins_when_a_touch_could_join_two() -> None:
    """A high in tolerance of two separate levels joins the closer one, deterministically."""
    detector = LiquidityDetector(tolerance=Decimal("5"))
    detector.update(_swing(SwingKind.HIGH, "100", 0), 0)  # cluster A, level 100
    detector.update(_swing(SwingKind.HIGH, "106", 1), 1)  # cluster B, level 106 (|106-100|=6 > 5)
    pool = detector.update(_swing(SwingKind.HIGH, "102", 2), 2)  # closer to A (2) than B (4)

    assert pool is not None
    assert pool.level == Decimal("102")  # joined A; its extreme rises to 102
    assert len(pool.touches) == 2
    assert pool.touches[0].price == Decimal("100")


def test_equidistant_levels_break_the_tie_to_the_older_pool() -> None:
    """When a new swing sits the same distance from two anchors, it joins the older level."""
    detector = LiquidityDetector(tolerance=Decimal("5"))
    detector.update(_swing(SwingKind.HIGH, "100", 0), 0)  # older, anchor 100
    detector.update(_swing(SwingKind.HIGH, "106", 1), 1)  # newer, anchor 106 (|106-100|=6 > 5)
    pool = detector.update(_swing(SwingKind.HIGH, "103", 2), 2)  # |103-100| == |103-106| == 3

    assert pool is not None
    assert pool.touches[0].price == Decimal("100")  # tie broke to the older level
    assert pool.level == Decimal("103")


def test_zero_tolerance_pools_only_exactly_equal_levels() -> None:
    """With tolerance 0 only identical levels stack; a one-point gap makes two lone levels."""
    exact = LiquidityDetector(tolerance=Decimal("0"))
    assert exact.update(_swing(SwingKind.HIGH, "100", 0), 0) is None
    pool = exact.update(_swing(SwingKind.HIGH, "100", 1), 1)  # exactly equal -> pool
    assert pool is not None
    assert pool.level == Decimal("100")
    assert len(pool.touches) == 2

    apart = LiquidityDetector(tolerance=Decimal("0"))
    apart.update(_swing(SwingKind.HIGH, "100", 0), 0)
    assert apart.update(_swing(SwingKind.HIGH, "101", 1), 1) is None  # |101-100|=1 > 0


def test_liquidity_detector_rejects_invalid_config() -> None:
    """Guardrails: a negative tolerance, a pool of fewer than two, or a zero window are errors."""
    with pytest.raises(ValueError, match="tolerance"):
        LiquidityDetector(tolerance=Decimal("-1"))
    with pytest.raises(ValueError, match="2 touches"):
        LiquidityDetector(tolerance=Decimal("1"), min_touches=1)
    with pytest.raises(ValueError, match="lookback"):
        LiquidityDetector(tolerance=Decimal("1"), lookback_bars=0)


@given(deltas=st.lists(st.integers(min_value=-3, max_value=3), min_size=2, max_size=8))
def test_highs_within_tolerance_of_the_anchor_collapse_to_one_pool_at_their_max(
    deltas: list[int],
) -> None:
    """A run of highs all within tolerance of the first touch is one pool whose level is their max.

    The anchor is the first high; deltas span at most 6 (from -3 to 3), so every high stays within
    the 6-point tolerance of that anchor and joins the single pool, whose reported level is the
    highest touch."""
    detector = LiquidityDetector(tolerance=Decimal("6"))  # span <= 6 keeps them one cluster
    base = 100
    last: LiquidityPool | None = None
    for index, delta in enumerate(deltas):
        last = detector.update(_swing(SwingKind.HIGH, str(base + delta), index), index)

    assert last is not None
    assert last.level == Decimal(base + max(deltas))
    assert len(last.touches) == len(deltas)


# --- Sweeps -------------------------------------------------------------------------------------
#
# The author's wedge, bar by bar: minor pivot lows 90, 92, 94.5, 97 climb while the corrections
# that separate them shrink monotonically (94-92=2.0, 96-94.5=1.5, 98-97=1.0). That is price
# grinding into a shelf of stops with less and less give — the setup a sweep needs before it means
# anything.
_WEDGE = [
    bar(0, open_="92", close="92", high="93", low="91"),  # lead-in
    bar(1, open_="92", close="91", high="92", low="90"),  # low 1: 90
    bar(2, open_="93", close="93.5", high="94", low="93"),  # high 94
    bar(3, open_="93", close="92.5", high="93", low="92"),  # low 2: 92    correction 2.0
    bar(4, open_="95", close="95.5", high="96", low="95"),  # high 96
    bar(5, open_="95", close="95", high="95.5", low="94.5"),  # low 3: 94.5 correction 1.5
    bar(6, open_="97.5", close="97.8", high="98", low="97.5"),  # high 98
    bar(7, open_="97.5", close="97.2", high="97.5", low="97"),  # low 4: 97   correction 1.0
    bar(8, open_="99", close="99.8", high="100", low="99"),  # wedge complete
]

_POOL_101 = LiquidityPool(
    side=LiquiditySide.BUY_SIDE,
    level=Decimal("101"),
    touches=(_swing(SwingKind.HIGH, "101", 0), _swing(SwingKind.HIGH, "100.5", 1)),
    time=_at(1),
)


def _sweeps(
    detector: SweepDetector, pools: LiquidityPool | list[LiquidityPool], candles: list[Candle]
) -> list[tuple[int, Sweep]]:
    """Track the pool(s), feed the candles in order; return (bar_index, sweep) for each sweep."""
    for pool in pools if isinstance(pools, list) else [pools]:
        detector.track(pool)
    found: list[tuple[int, Sweep]] = []
    for index, candle in enumerate(candles):
        found.extend((index, sweep) for sweep in detector.update(candle))
    return found


def test_sweep_golden_the_authors_wedge_pierces_the_pool_and_closes_back_inside() -> None:
    """Author's example: the wedge climbs into a pool at 101, b9 wicks to 102, b10 closes back in.

    This is the case the whole primitive exists for, and it pins the two rules that separate a
    sweep from a break of structure. b9 goes *through* 101 (high 102) but closes at 101.5 — above
    the level, so nothing is decided yet. b10 closes at 100.5, back inside: the stops above 101
    were filled and the level was rejected. Had price instead held above 101 for the whole window,
    the same wick would have been acceptance, not a trap.

    The reported wedge is the four rising lows — the trendline of stops the cascade will now run
    through — and `extreme` is 102, the furthest the raid reached.
    """
    candles = [
        *_WEDGE,
        bar(9, open_="100", close="101.5", high="102", low="99.5"),  # pierces, closes above
        bar(10, open_="101", close="100.5", high="101.6", low="100"),  # closes back inside
    ]
    sweeps = _sweeps(SweepDetector(), _POOL_101, candles)

    assert len(sweeps) == 1
    index, sweep = sweeps[0]
    assert index == 10  # confirmed on the bar that closed back inside, not the bar that pierced
    assert sweep.side is LiquiditySide.BUY_SIDE
    assert sweep.level == Decimal("101")
    assert sweep.extreme == Decimal("102")
    assert sweep.pierced_at == _at(9)
    assert sweep.time == _at(10)
    assert [low.price for low in sweep.wedge] == [
        Decimal("90"),
        Decimal("92"),
        Decimal("94.5"),
        Decimal("97"),
    ]


def test_a_pierce_and_a_recovery_on_the_same_bar_is_a_sweep() -> None:
    """One bar can do both: wick through the level and close back inside before it ever ends."""
    candles = [*_WEDGE, bar(9, open_="100", close="100", high="102", low="99")]
    sweeps = _sweeps(SweepDetector(), _POOL_101, candles)

    assert len(sweeps) == 1
    _, sweep = sweeps[0]
    assert sweep.pierced_at == sweep.time == _at(9)


def test_the_wick_keeps_extending_while_the_window_is_open() -> None:
    """`extreme` is the furthest point of the whole raid, not just the bar that first pierced."""
    candles = [
        *_WEDGE,
        bar(9, open_="100", close="101.2", high="101.5", low="99.5"),  # pierces to 101.5
        bar(10, open_="101", close="100.5", high="103", low="100"),  # runs to 103, then closes in
    ]
    sweeps = _sweeps(SweepDetector(), _POOL_101, candles)

    assert len(sweeps) == 1
    _, sweep = sweeps[0]
    assert sweep.extreme == Decimal("103")
    assert sweep.pierced_at == _at(9)


def test_a_level_held_past_the_window_is_acceptance_not_a_sweep() -> None:
    """Three bars without a close back inside means the market took the level and kept it.

    With `recovery_bars=3` the window covers b9, b10 and b11. Price closes above 101 on all three,
    so by b12 the pool is gone — and the close back below on b12 reports nothing. That deadline is
    what keeps a sweep meaningful: without it every break of structure would eventually be
    relabelled a sweep by a late enough pullback.
    """
    candles = [
        *_WEDGE,
        bar(9, open_="100", close="101.5", high="102", low="99.5"),  # pierce, window opens
        bar(10, open_="101.5", close="101.8", high="102", low="101.2"),
        bar(11, open_="101.8", close="102", high="102.5", low="101.5"),  # window closes here
        bar(12, open_="101.5", close="100.5", high="102", low="100"),  # too late
    ]
    assert _sweeps(SweepDetector(), _POOL_101, candles) == []


def test_a_close_exactly_at_the_level_has_not_recovered() -> None:
    """Strict comparisons, as everywhere else here: a close *at* 101 is neither in nor out."""
    candles = [
        *_WEDGE,
        bar(9, open_="100", close="101.5", high="102", low="99.5"),
        bar(10, open_="101", close="101", high="101.6", low="100.5"),  # exactly at the level
        bar(11, open_="101", close="100", high="101.2", low="100"),  # strictly inside
    ]
    sweeps = _sweeps(SweepDetector(), _POOL_101, candles)

    assert [index for index, _ in sweeps] == [11]


def test_a_pierce_without_a_wedge_is_not_a_sweep() -> None:
    """The wedge is a precondition: a wick through a level out of nowhere reports nothing.

    These bars walk straight up to the pool with no minor pivots at all — no rising lows, no
    shrinking corrections, no trendline of trapped stops. Price closes inside the level first (so
    the pool is live and armed), then pierces 101 and closes back inside, and the detector still
    stays silent. Only the missing wedge separates this from the golden.
    """
    candles = [
        bar(0, open_="98", close="98.5", high="99", low="98"),
        bar(1, open_="98.5", close="99.5", high="100", low="98.5"),
        bar(2, open_="99.5", close="100", high="100.5", low="99.5"),  # closes inside: pool armed
        bar(3, open_="100", close="101.5", high="102", low="100"),  # pierces
        bar(4, open_="101.5", close="100.5", high="101.5", low="100"),  # closes back inside
    ]
    assert _sweeps(SweepDetector(), _POOL_101, candles) == []


def test_a_correction_that_grows_breaks_the_wedge() -> None:
    """Corrections must shrink monotonically — 2.0, 1.0, 1.5 is not a wedge losing volatility.

    The lows still rise (90, 92, 94.5, 97), so a rule that only checked the rising trendline would
    accept this. It is the volatility decay that makes the pattern a squeeze rather than an
    ordinary uptrend, and here only two lows survive the scan back — one short of the minimum.
    """
    candles = [
        bar(0, open_="92", close="92", high="93", low="91"),
        bar(1, open_="92", close="91", high="92", low="90"),  # low 90
        bar(2, open_="93", close="93.5", high="94", low="93"),  # high 94
        bar(3, open_="93", close="92.5", high="93", low="92"),  # low 92    correction 2.0
        bar(4, open_="95", close="95.2", high="95.5", low="95"),  # high 95.5
        bar(5, open_="95", close="94.8", high="95.2", low="94.5"),  # low 94.5 correction 1.0
        bar(6, open_="97.5", close="98", high="98.5", low="97.5"),  # high 98.5
        bar(7, open_="97.5", close="97.2", high="97.5", low="97"),  # low 97   correction 1.5 (grew)
        bar(8, open_="99", close="99.8", high="100", low="99"),
        bar(9, open_="100", close="101.5", high="102", low="99.5"),
        bar(10, open_="101", close="100.5", high="101.6", low="100"),
    ]
    assert _sweeps(SweepDetector(), _POOL_101, candles) == []


def test_two_rising_lows_are_one_short_of_a_wedge() -> None:
    """Three lows are the floor: two lows show one correction, and one correction cannot shrink."""
    candles = [
        bar(0, open_="93", close="93.5", high="94", low="93"),
        bar(1, open_="93", close="92.5", high="93", low="92"),  # low 92
        bar(2, open_="95", close="95.5", high="96", low="95"),  # high 96
        bar(3, open_="95", close="95", high="95.5", low="94.5"),  # low 94.5 — only two lows
        bar(4, open_="99", close="99.8", high="100", low="99"),
        bar(5, open_="100", close="101.5", high="102", low="99.5"),
        bar(6, open_="101", close="100.5", high="101.6", low="100"),
    ]
    assert _sweeps(SweepDetector(), _POOL_101, candles) == []


def test_the_bearish_mirror_sweeps_a_sell_side_pool() -> None:
    """The author's wedge reflected: falling highs, shrinking rallies, a wick below and a close in.

    Every bar is the golden mirrored about 200, so the highs descend 110, 108, 105.5, 103 with
    rallies of 2.0, 1.5, 1.0. The pool of sell stops sits at 99; b9 wicks down to 98 and b10 closes
    back above. Same machine, opposite sign — which is the point of testing it: the sell side must
    not be a second, subtly different implementation.
    """
    pool = LiquidityPool(
        side=LiquiditySide.SELL_SIDE,
        level=Decimal("99"),
        touches=(_swing(SwingKind.LOW, "99", 0), _swing(SwingKind.LOW, "99.5", 1)),
        time=_at(1),
    )
    candles = [
        bar(0, open_="108", close="108", high="109", low="107"),
        bar(1, open_="108", close="109", high="110", low="108"),  # high 110
        bar(2, open_="107", close="106.5", high="107", low="106"),  # low 106
        bar(3, open_="107", close="107.5", high="108", low="107"),  # high 108   rally 2.0
        bar(4, open_="105", close="104.5", high="105", low="104"),  # low 104
        bar(5, open_="105", close="105", high="105.5", low="104.5"),  # high 105.5 rally 1.5
        bar(6, open_="102.5", close="102.2", high="102.5", low="102"),  # low 102
        bar(7, open_="102.5", close="102.8", high="103", low="102.5"),  # high 103  rally 1.0
        bar(8, open_="101", close="100.2", high="101", low="100"),
        bar(9, open_="100", close="98.5", high="100.5", low="98"),  # pierces below 99
        bar(10, open_="99", close="99.5", high="100", low="98.4"),  # closes back above
    ]
    sweeps = _sweeps(SweepDetector(), pool, candles)

    assert len(sweeps) == 1
    index, sweep = sweeps[0]
    assert index == 10
    assert sweep.side is LiquiditySide.SELL_SIDE
    assert sweep.extreme == Decimal("98")
    assert [high.price for high in sweep.wedge] == [
        Decimal("110"),
        Decimal("108"),
        Decimal("105.5"),
        Decimal("103"),
    ]


def test_a_deepened_pool_replaces_its_earlier_level() -> None:
    """Re-tracking a pool that gained a touch moves the line to defend instead of watching both."""
    detector = SweepDetector()
    detector.track(_POOL_101)
    deeper = LiquidityPool(
        side=LiquiditySide.BUY_SIDE,
        level=Decimal("103"),  # a third equal high lifted the extreme
        touches=(*_POOL_101.touches, _swing(SwingKind.HIGH, "103", 2)),
        time=_at(2),
    )
    detector.track(deeper)

    for candle in _WEDGE:
        assert detector.update(candle) == ()
    # 102 pierced the old level but not the new one, so nothing arms and nothing is reported.
    assert detector.update(bar(9, open_="100", close="101.5", high="102", low="99.5")) == ()
    assert detector.update(bar(10, open_="101", close="100.5", high="101.6", low="100")) == ()


def test_a_longer_recovery_window_catches_a_slower_trap() -> None:
    """`recovery_bars` is the knob: the same bars that time out at 3 still confirm at 5."""
    candles = [
        *_WEDGE,
        bar(9, open_="100", close="101.5", high="102", low="99.5"),
        bar(10, open_="101.5", close="101.8", high="102", low="101.2"),
        bar(11, open_="101.8", close="102", high="102.5", low="101.5"),
        bar(12, open_="101.5", close="100.5", high="102", low="100"),
    ]
    assert _sweeps(SweepDetector(), _POOL_101, candles) == []

    patient = _sweeps(SweepDetector(recovery_bars=5), _POOL_101, candles)
    assert [index for index, _ in patient] == [12]


def test_a_pool_goes_stale_and_stops_being_watched() -> None:
    """A pool nothing has approached for `lookback_bars` is dropped rather than watched forever."""
    detector = SweepDetector(lookback_bars=4)
    detector.track(_POOL_101)
    for candle in _WEDGE:  # nine quiet bars, well past the four-bar window
        assert detector.update(candle) == ()
    assert detector.update(bar(9, open_="100", close="101.5", high="102", low="99.5")) == ()
    assert detector.update(bar(10, open_="101", close="100.5", high="101.6", low="100")) == ()


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"recovery_bars": 0}, "recovery_bars must be >= 1"),
        ({"min_wedge_pivots": 2}, "a wedge needs at least 3 pivots"),
        ({"lookback_bars": 0}, "lookback_bars must be >= 1"),
    ],
)
def test_nonsensical_sweep_settings_are_refused(kwargs: dict[str, int], message: str) -> None:
    """Bad configuration fails at construction, not silently at the first missed sweep."""
    with pytest.raises(ValueError, match=message):
        SweepDetector(**kwargs)


def test_two_lows_without_a_high_between_them_collapse_to_the_lower() -> None:
    """A doubled low is one pivot, not two — the wedge measures from 90, never from 91.

    Bars 1 and 3 both print minor lows (90, then 91) with no minor high between them, because bar
    2's high never clears its neighbours. They belong to the same leg down, so the sequence has to
    keep the extreme and drop the other; otherwise the wedge would later measure a "correction"
    across two lows with no high dividing them. The assertion is the wedge's first low: 90 if the
    zig-zag collapsed correctly, 91 if both were kept.
    """
    candles = [
        bar(0, open_="93", close="93", high="95", low="91"),
        bar(1, open_="93", close="92", high="95", low="90"),  # low 90
        bar(2, open_="93", close="93.5", high="94", low="93"),  # no high: 94 < bar 1's 95
        bar(3, open_="93", close="92", high="93", low="91"),  # low 91 — same leg, discarded
        bar(4, open_="93", close="93.5", high="94", low="93"),  # high 94
        bar(5, open_="93", close="92.5", high="93", low="92"),  # low 92     correction 2.0
        bar(6, open_="95", close="95.5", high="96", low="95"),  # high 96
        bar(7, open_="95", close="95", high="95.5", low="94.5"),  # low 94.5 correction 1.5
        bar(8, open_="97.5", close="97.8", high="98", low="97.5"),  # high 98
        bar(9, open_="97.5", close="97.2", high="97.5", low="97"),  # low 97   correction 1.0
        bar(10, open_="99", close="99.8", high="100", low="99"),
        bar(11, open_="100", close="101.5", high="102", low="99.5"),  # pierces
        bar(12, open_="101", close="100.5", high="101.6", low="100"),  # closes back inside
    ]
    sweeps = _sweeps(SweepDetector(), _POOL_101, candles)

    assert len(sweeps) == 1
    _, sweep = sweeps[0]
    assert [low.price for low in sweep.wedge] == [
        Decimal("90"),
        Decimal("92"),
        Decimal("94.5"),
        Decimal("97"),
    ]


def test_the_pivot_history_stays_bounded_over_a_long_series() -> None:
    """A wedge only ever reads a tail of the zig-zag, so the history must not grow with the series.

    Without the cap a live session would accumulate one pivot every few bars forever. This drives a
    clean sawtooth: each even bar sits entirely above its neighbours (a minor high) and each odd
    bar entirely below (a minor low), so the sequence grows by one pivot per bar and comfortably
    passes the cap. Deliberately *not* outside bars — those collapse into the previous pivot, which
    would keep the sequence short and quietly stop this test from exercising the cap at all.
    """
    tracker = _WedgeTracker(min_pivots=3)
    for index in range(140):
        low = index % 2 == 1
        tracker.update(
            bar(
                index,
                open_="98" if low else "100",
                close="98" if low else "100",
                high="99" if low else "101",
                low="98" if low else "100",
            )
        )

    assert len(tracker._pivots) > 1  # the series really did produce pivots to cap
    assert len(tracker._pivots) <= _WedgeTracker._MAX_PIVOTS
    # A flat sawtooth has no shrinking corrections, so it is still correctly not a wedge.
    assert tracker.bullish_wedge() is None
    assert tracker.bearish_wedge() is None


def test_a_doubled_low_keeps_the_later_pivot_when_that_one_is_deeper() -> None:
    """The mirror of the collapse: 91 then 90 keeps 90, so the extreme wins regardless of order.

    Same shape as the test above with the two lows swapped. Whichever arrives first, the leg's real
    pivot is its lowest point — a rule that just kept the first (or just kept the last) would put
    the wedge's trendline in the wrong place half the time.
    """
    candles = [
        bar(0, open_="93", close="93", high="95", low="92"),
        bar(1, open_="93", close="92", high="95", low="91"),  # low 91
        bar(2, open_="93", close="93.5", high="94", low="93"),  # no high: 94 < bar 1's 95
        bar(3, open_="93", close="92", high="93", low="90"),  # low 90 — deeper, so it replaces 91
        bar(4, open_="93", close="93.5", high="94", low="93"),  # high 94
        bar(5, open_="93", close="92.5", high="93", low="92"),  # low 92     correction 2.0
        bar(6, open_="95", close="95.5", high="96", low="95"),  # high 96
        bar(7, open_="95", close="95", high="95.5", low="94.5"),  # low 94.5 correction 1.5
        bar(8, open_="97.5", close="97.8", high="98", low="97.5"),  # high 98
        bar(9, open_="97.5", close="97.2", high="97.5", low="97"),  # low 97   correction 1.0
        bar(10, open_="99", close="99.8", high="100", low="99"),
        bar(11, open_="100", close="101.5", high="102", low="99.5"),  # pierces
        bar(12, open_="101", close="100.5", high="101.6", low="100"),  # closes back inside
    ]
    sweeps = _sweeps(SweepDetector(), _POOL_101, candles)

    assert len(sweeps) == 1
    _, sweep = sweeps[0]
    assert sweep.wedge[0].price == Decimal("90")


def test_a_pool_the_market_already_broke_cannot_be_swept() -> None:
    """Price trading *above* a buy-side pool means those stops are long gone — nothing left to raid.

    Every bar here is the golden wedge shifted up 15 points, so the whole approach happens between
    105 and 113 and never once closes below the pool at 101. The wedge is valid, the highs are far
    above the level, and bar 6 eventually sells off through 101. That is a market breaking down
    through an old, already-taken level — not a trap. Reporting it would hand the strategy a
    reversal signal 13 points into the move, systematically at the worst possible price.
    """
    candles = [
        bar(0, open_="107", close="107", high="108", low="106"),
        bar(1, open_="107", close="106", high="107", low="105"),  # low 105
        bar(2, open_="108", close="108.5", high="109", low="108"),  # high 109
        bar(3, open_="108", close="107.5", high="108", low="107"),  # low 107     correction 2.0
        bar(4, open_="110", close="110.5", high="111", low="110"),  # high 111
        bar(5, open_="110", close="110", high="110.5", low="109.5"),  # low 109.5 correction 1.5
        bar(6, open_="112.5", close="112.8", high="113", low="112.5"),  # wedge valid, high 113
        bar(7, open_="112.5", close="100", high="113", low="99.5"),  # sells through 101
    ]
    assert _sweeps(SweepDetector(), _POOL_101, candles) == []


def test_one_bar_sweeping_two_pools_reports_both() -> None:
    """A single push can clear stops at 101 and at 103 — both are real events, both come back.

    Reporting only one would silently lose the other, and a strategy anchored on the deeper pool
    would simply never fire. They are returned ordered by level, so the output does not depend on
    which pool happened to be tracked first.
    """
    higher = LiquidityPool(  # its own first touch, so it is a distinct pool, not the same one
        side=LiquiditySide.BUY_SIDE,
        level=Decimal("103"),
        touches=(_swing(SwingKind.HIGH, "103", 2), _swing(SwingKind.HIGH, "102.5", 3)),
        time=_at(3),
    )
    candles = [*_WEDGE, bar(9, open_="100", close="100.5", high="104", low="99.5")]

    for order in ([_POOL_101, higher], [higher, _POOL_101]):
        sweeps = _sweeps(SweepDetector(), order, candles)
        assert [(index, sweep.level) for index, sweep in sweeps] == [
            (9, Decimal("101")),
            (9, Decimal("103")),
        ]
        # Each carries its own extent: the raid ran to 104 past both levels.
        assert {sweep.extreme for _, sweep in sweeps} == {Decimal("104")}


def test_a_pool_cannot_be_swept_by_the_bar_that_created_it() -> None:
    """The bar that completes a pool has only just closed — it cannot also have raided it.

    A caller wiring `LiquidityDetector` into `SweepDetector` naturally tracks the pool and feeds
    the same candle in the same iteration. Without the guard, bar 9's high would sweep a level that
    bar 9 itself had just established: a decision using information from its own bar, which is the
    anti-lookahead invariant broken outright.
    """
    born_on_bar_9 = LiquidityPool(
        side=LiquiditySide.BUY_SIDE,
        level=Decimal("101"),
        touches=(_swing(SwingKind.HIGH, "101", 7), _swing(SwingKind.HIGH, "100.5", 9)),
        time=_at(9),
    )
    detector = SweepDetector()
    for candle in _WEDGE:
        detector.update(candle)
    detector.track(born_on_bar_9)

    assert detector.update(bar(9, open_="100", close="100", high="102", low="99")) == ()


def test_a_recovery_window_of_one_bar_allows_no_follow_through() -> None:
    """`recovery_bars=1` is the strictest setting: the piercing bar must close back inside itself.

    The golden's b9 pierces and closes *above* 101, so with a one-bar window it has already failed
    by the time b10 drags price back. The same bars confirm at the default of 3.
    """
    candles = [
        *_WEDGE,
        bar(9, open_="100", close="101.5", high="102", low="99.5"),
        bar(10, open_="101", close="100.5", high="101.6", low="100"),
    ]
    assert _sweeps(SweepDetector(recovery_bars=1), _POOL_101, candles) == []
    assert [index for index, _ in _sweeps(SweepDetector(), _POOL_101, candles)] == [10]


def test_the_wedge_may_be_completed_by_the_piercing_bar_itself() -> None:
    """The last low can confirm on the very bar that springs the trap — and that is not lookahead.

    A minor pivot needs one bar on each side, so the wedge's final low (bar 7) is only confirmed
    when bar 8 closes. Here bar 8 is also the bar that pierces 101. Both facts are known at bar 8's
    close, and the sweep it completes acts at bar 9's open, so the invariant holds: the wedge never
    contains a pivot from a bar that has not closed.
    """
    candles = [
        *_WEDGE[:8],  # through bar 7 — the wedge's last low, not yet confirmed
        bar(8, open_="99", close="101.5", high="102", low="99"),  # confirms low 97 *and* pierces
        bar(9, open_="101", close="100.5", high="101.6", low="100"),  # closes back inside
    ]
    sweeps = _sweeps(SweepDetector(), _POOL_101, candles)

    assert len(sweeps) == 1
    index, sweep = sweeps[0]
    assert index == 9
    assert sweep.pierced_at == _at(8)
    assert [low.price for low in sweep.wedge] == [
        Decimal("90"),
        Decimal("92"),
        Decimal("94.5"),
        Decimal("97"),
    ]


def test_an_outside_bar_does_not_become_its_own_correction() -> None:
    """An outside bar prints both extremes; taking both would measure a correction inside one bar.

    Bar 4 engulfs its neighbours, so `SwingDetector` confirms a high *and* a low on it. If both
    entered the zig-zag, the "correction" between the lows either side would be that single bar's
    range rather than a move between legs — a wedge measured against noise. Only the pivot that
    continues the alternation is kept, so the sequence stays a strict zig-zag and the corrections
    stay the 2.0 / 1.5 / 1.0 the golden expects.
    """
    tracker = _WedgeTracker(min_pivots=3)
    candles = [
        *_WEDGE[:4],
        bar(4, open_="95", close="95.5", high="96", low="88"),  # outside bar: high 96 and low 88
        *_WEDGE[5:],
    ]
    for candle in candles:
        tracker.update(candle)

    kinds = [pivot.kind for pivot in tracker._pivots]
    assert all(a is not b for a, b in pairwise(kinds))  # strictly alternating


@given(
    bars=st.lists(
        st.tuples(st.integers(min_value=0, max_value=40), st.integers(min_value=0, max_value=12)),
        min_size=6,
        max_size=40,
    ),
    level=st.integers(min_value=5, max_value=35),
)
def test_every_reported_sweep_pierced_the_level_and_closed_back_inside(
    bars: list[tuple[int, int]], level: int
) -> None:
    """Over random series: whatever comes out really is a wick beyond the level and a close inside.

    The two defining facts of a sweep, asserted directly rather than through a hand-built series —
    `extreme` strictly beyond `level`, the closing bar strictly inside it, and the pierce never
    later than the confirmation. A regression that reported a break as a sweep, or stamped
    `pierced_at` with a bar that never went through, fails here on some series even if every
    golden still passes.
    """
    candles = [
        bar(index, open_=str(low), close=str(low), high=str(low + span), low=str(low))
        for index, (low, span) in enumerate(bars)
    ]
    pool = LiquidityPool(
        side=LiquiditySide.BUY_SIDE,
        level=Decimal(level),
        touches=(_swing(SwingKind.HIGH, str(level), 0), _swing(SwingKind.HIGH, str(level), 0)),
        time=_at(0) - HOUR,  # older than every candle, so nothing is blocked by the birth guard
    )
    by_time = {candle.time: candle for candle in candles}

    for _, sweep in _sweeps(SweepDetector(), pool, candles):
        assert sweep.extreme > sweep.level  # the wick really went through
        assert by_time[sweep.time].close < sweep.level  # and price really came back
        assert sweep.pierced_at <= sweep.time
        assert by_time[sweep.pierced_at].high > sweep.level  # the stamped bar really pierced
        # The raid came from the protected side: some earlier bar closed at or inside the level.
        # Without this the whole class of "pool the market broke long ago" passes every other
        # assertion here — its piercing bar's high clears the level too, from above.
        assert any(
            candle.close <= sweep.level for candle in candles if candle.time < sweep.pierced_at
        )


@given(
    bars=st.lists(
        st.tuples(st.integers(min_value=0, max_value=40), st.integers(min_value=0, max_value=12)),
        min_size=6,
        max_size=40,
    ),
    level=st.integers(min_value=5, max_value=35),
)
def test_the_same_series_always_produces_the_same_sweeps(
    bars: list[tuple[int, int]], level: int
) -> None:
    """Determinism, the engine's second invariant: same input, same output, every run."""
    candles = [
        bar(index, open_=str(low), close=str(low), high=str(low + span), low=str(low))
        for index, (low, span) in enumerate(bars)
    ]
    pool = LiquidityPool(
        side=LiquiditySide.BUY_SIDE,
        level=Decimal(level),
        touches=(_swing(SwingKind.HIGH, str(level), 0), _swing(SwingKind.HIGH, str(level), 0)),
        time=_at(0) - HOUR,
    )

    assert _sweeps(SweepDetector(), pool, candles) == _sweeps(SweepDetector(), pool, candles)


def test_re_tracking_a_pool_at_the_same_level_keeps_what_it_already_knows() -> None:
    """A pool that deepens without moving is the same line, so its armed state must survive.

    `LiquidityDetector` re-reports a pool every time a touch is added. If that reset the "price is
    inside" flag, a pool touched again just before the raid would be disarmed at exactly the moment
    it mattered, and the sweep would be missed.
    """
    detector = SweepDetector()
    detector.track(_POOL_101)
    for candle in _WEDGE:  # price closes inside 101 throughout: the pool arms
        detector.update(candle)

    deepened = LiquidityPool(  # a third touch, same extreme, same first touch -> same pool
        side=LiquiditySide.BUY_SIDE,
        level=Decimal("101"),
        touches=(*_POOL_101.touches, _swing(SwingKind.HIGH, "99", 2)),
        time=_at(2),
    )
    detector.track(deepened)

    assert detector.update(bar(9, open_="100", close="101.5", high="102", low="99.5")) == ()
    sweeps = detector.update(bar(10, open_="101", close="100.5", high="101.6", low="100"))
    assert [sweep.level for sweep in sweeps] == [Decimal("101")]


def test_a_pool_can_be_raided_on_the_very_next_bar_after_it_is_tracked() -> None:
    """A pool confirmed at one bar's close and raided at the next is the *cleanest* case, not a
    corner one — the detector must not need a warm-up bar to see it.

    This is the real call order: feed the bar, track what it produced, feed the next bar. Arming a
    new pool from `False` would blind it for exactly one candle and lose precisely this pattern,
    while the very same series still reported a sweep if the pool happened to be tracked earlier.
    Same candles, same pool, same answer, regardless of when tracking began.
    """
    late = SweepDetector()
    for candle in _WEDGE:
        late.update(candle)
    late.track(_POOL_101)  # only known now, at the close of bar 8
    assert late.update(bar(9, open_="100", close="101.5", high="102", low="99.5")) == ()
    swept = late.update(bar(10, open_="101", close="100.5", high="101.6", low="100"))
    assert [sweep.level for sweep in swept] == [Decimal("101")]

    candles = [
        *_WEDGE,
        bar(9, open_="100", close="101.5", high="102", low="99.5"),
        bar(10, open_="101", close="100.5", high="101.6", low="100"),
    ]
    early = _sweeps(SweepDetector(), _POOL_101, candles)
    assert [index for index, _ in early] == [10]


def test_a_pool_that_deepens_to_a_new_level_stays_armed() -> None:
    """A triple top swept: two equal highs at 101, a third at 102, and the next bar raids 102.

    When the level moves the pierce in flight is rightly discarded — it was aimed at another line.
    But the *arming* must carry over: a buy-side level only ever rises, so price sitting below 101
    is a fortiori below 102. Re-arming from scratch here would kill the highest-quality instance of
    the pattern the primitive detects.
    """
    detector = SweepDetector()
    detector.track(_POOL_101)
    for candle in _WEDGE:
        detector.update(candle)

    raised = LiquidityPool(  # same pool, third touch lifts the extreme to 102
        side=LiquiditySide.BUY_SIDE,
        level=Decimal("102"),
        touches=(*_POOL_101.touches, _swing(SwingKind.HIGH, "102", 6)),
        time=_at(6),
    )
    detector.track(raised)

    assert detector.update(bar(9, open_="100", close="102.5", high="103", low="99.5")) == ()
    swept = detector.update(bar(10, open_="102", close="101", high="102.4", low="100"))
    assert [sweep.level for sweep in swept] == [Decimal("102")]


def test_a_close_exactly_at_the_level_leaves_the_pool_armed() -> None:
    """Sitting *on* the level is not acceptance, so a doji there must not disarm the pool.

    `inside` deliberately uses a non-strict comparison while the pierce and the recovery stay
    strict — they answer different questions. Round numbers are where stops gather and where a
    close lands exactly on the level, so reusing the strict test would disarm pools precisely
    where the pattern matters most.
    """
    candles = [
        *_WEDGE[:8],
        bar(8, open_="100.5", close="101", high="101", low="100"),  # closes exactly on 101
        bar(9, open_="101", close="101.5", high="102", low="100.5"),  # pierces
        bar(10, open_="101", close="100.5", high="101.6", low="100"),  # closes back inside
    ]
    sweeps = _sweeps(SweepDetector(), _POOL_101, candles)

    assert [index for index, _ in sweeps] == [10]


def test_pools_sharing_a_level_and_side_are_ordered_by_their_first_touch() -> None:
    """Two distinct pools can settle on the same price, so level and side alone do not order them.

    An aged-out cluster and a fresh one can both sit at 101. Sorting on `(level, side)` alone would
    leave the tie to insertion order, making the output depend on which was tracked first — the
    exact non-determinism the sort exists to remove. The first touch is the tiebreak.
    """
    twin = LiquidityPool(
        side=LiquiditySide.BUY_SIDE,
        level=Decimal("101"),
        touches=(_swing(SwingKind.HIGH, "101", 4), _swing(SwingKind.HIGH, "100.8", 5)),
        time=_at(5),
    )
    candles = [
        *_WEDGE,
        bar(9, open_="100", close="101.5", high="102", low="99.5"),
        bar(10, open_="101", close="100.5", high="101.6", low="100"),
    ]

    for order in ([_POOL_101, twin], [twin, _POOL_101]):
        sweeps = _sweeps(SweepDetector(), order, candles)
        assert [sweep.pool.touches[0].time for _, sweep in sweeps] == [_at(0), _at(4)]


def test_an_outside_bar_keeps_its_extreme_instead_of_dropping_it() -> None:
    """The outside bar's new high must survive, or the next correction is measured too small.

    Bar 4 makes both a higher high (99) and a lower low than its neighbours. Discarding the high
    because it does not alternate would leave the previous, lower high in place, so the following
    correction (high minus next low) would be understated — and a wedge is defined by corrections
    that *shrink*, so understating one can manufacture a shrink that never happened. The sequence
    must stay strictly alternating and still carry 99.
    """
    tracker = _WedgeTracker(min_pivots=3)
    candles = [
        *_WEDGE[:4],
        bar(4, open_="95", close="95.5", high="99", low="88"),  # outside bar: high 99, low 88
        *_WEDGE[5:],
    ]
    for candle in candles:
        tracker.update(candle)

    kinds = [pivot.kind for pivot in tracker._pivots]
    assert all(a is not b for a, b in pairwise(kinds))  # still a strict zig-zag
    assert Decimal("99") in [pivot.price for pivot in tracker._pivots]  # the real high survived


def test_an_outside_bar_cannot_open_the_zig_zag() -> None:
    """With no earlier pivot there is no tail to order the pair against, so neither is taken.

    Accepting both would seed the sequence with a high and a low from the *same* bar — precisely
    the degenerate shape the ordering rule exists to prevent — and there is no principled way to
    pick one without a tail to alternate from. The sequence starts at the next unambiguous pivot.
    """
    tracker = _WedgeTracker(min_pivots=3)
    for candle in [
        bar(0, open_="93", close="93", high="95", low="92"),
        bar(1, open_="93", close="93", high="99", low="88"),  # outside bar, first pivot of all
        bar(2, open_="93", close="93", high="96", low="91"),
    ]:
        tracker.update(candle)

    assert tracker._pivots == []


# --- Order blocks -------------------------------------------------------------------------------
#
# The author's validated example — three bars to set up the break, then the impulse that breaks it
# while leaving two gap events separated by a pause. The bar-by-bar reading of why it marks two
# regions and not three is on the fixture itself, in `tradeforge_engine.testing`; it moved there
# when the API's route tests came to need the same scenario.
_OB_IMPULSE = GAPPING_IMPULSE


def _zones(candles: list[Candle]) -> list[tuple[int, OrderBlock]]:
    """Drive structure and order blocks together the way a strategy would, one bar at a time."""
    structure, blocks = MarketStructure(), OrderBlockDetector()
    found: list[tuple[int, OrderBlock]] = []
    for candle in candles:
        break_ = structure.update(candle)
        found.extend((_index_of(candle), zone) for zone in blocks.update(candle, break_))
    return found


def _zones_from_bullish(candles: list[Candle]) -> list[tuple[int, OrderBlock]]:
    """`_zones`, on a machine already in an uptrend. See `BULLISH_START`.

    The prefix marks no zone of its own — its bars overlap throughout, so they leave no gap for
    one to be marked from — and the step up into bar 0 falls outside every break's impulse leg,
    so it marks nothing either. `test_the_bullish_start_marks_no_zone_of_its_own` pins both.
    """
    return [(index, zone) for index, zone in _zones([*BULLISH_START, *candles]) if index >= 0]


def test_the_bullish_start_marks_no_zone_of_its_own() -> None:
    """The prefix must be inert for order blocks, or `_zones_from_bullish` would hide a real one.

    Two ways it could contaminate a scenario, and both are checked. Its own bars could leave a gap
    and mark a zone — they do not, they overlap throughout. And the step up from bar -1 into bar 0
    is a gap by construction, so it could be picked up by the scenario's own break; it is not,
    because it sits before that break's origin and the detector only hunts inside the impulse leg.
    Were either to happen, the filter in `_zones_from_bullish` would quietly drop the first and
    the second would show up as a zone nobody put there.
    """
    assert _zones(BULLISH_START) == []
    assert [index for index, _ in _zones([*BULLISH_START, *_OB_IMPULSE])] == [9, 9]


def test_order_block_golden_one_impulse_two_gap_events_two_zones() -> None:
    """The author's example: an impulse gapping twice, with a pause between, marks exactly 2 zones.

    Both zones are the candle *before* their gap event, and both are widened by the gap candle's
    wick only where that wick ran deeper — here neither did, so each keeps its own low. The primary
    is the first event; the secondary is the one after the pause. Gaps A and B are adjacent bars,
    one continuous push, so they share a zone: without that rule this leg would mark three.
    """
    zones = _zones_from_bullish(_OB_IMPULSE)

    assert [(index, z.time, z.top, z.bottom, z.primary) for index, z in zones] == [
        (9, _at(3), Decimal("100"), Decimal("98"), True),  # from gap A, marked on bar 3
        (9, _at(7), Decimal("117"), Decimal("110"), False),  # from gap C, marked on bar 7
    ]
    assert all(z.kind is ZoneKind.DEMAND for _, z in zones)
    assert all(z.break_kind is StructureKind.BOS for _, z in zones)


def test_a_zone_belongs_to_its_own_bar_but_is_only_known_at_the_break() -> None:
    """The anti-lookahead pair, stated directly: drawn on bar 3, knowable only from bar 9.

    Nothing about bar 3 says "zone" while bar 3 is closing — it takes the break six bars later to
    make it one. A backtest that marked it on bar 3 would be trading on information the market had
    not yet produced.
    """
    index, zone = _zones_from_bullish(_OB_IMPULSE)[0]
    assert zone.time == _at(3)
    assert zone.confirmed_at == _at(9)
    assert index == 9
    assert zone.time < zone.confirmed_at


def test_adjacent_gaps_are_one_event_and_mark_from_the_first() -> None:
    """A run of gaps on consecutive bars is one push, so the zone is the candle before the *first*.

    Removing the pause from the golden leaves gaps on bars 5, 6 and 9 with no gap-free bar between
    5, 6 and 7 — one long run — and the leg must still mark a single zone, anchored at its start.
    """
    candles = [
        *_OB_IMPULSE[:7],
        bar(7, open_="112", close="112", high="117", low="111"),  # now gaps: 110 < 111
        bar(8, open_="116", close="116", high="118", low="116"),  # gaps: 115 < 116
        bar(9, open_="124", close="124", high="125", low="118"),  # gaps: 117 < 118, and BOS
    ]
    zones = _zones_from_bullish(candles)

    assert [(z.time, z.primary) for _, z in zones] == [(_at(3), True)]


def test_a_gap_outside_the_impulse_leg_marks_nothing() -> None:
    """Only the leg that broke structure leaves a footprint worth trading.

    The out-of-leg gap is the climb out of `BULLISH_START` and into bar 0 — a real bullish gap,
    of the same kind the break wants, sitting on bars 0 and 1 and therefore *before* the impulse's
    origin on bar 3. It is not the move that broke anything, so it marks nothing, and the two zones
    reported are the impulse's own.

    The exact list is asserted rather than `all(time >= origin)`: that weaker form is satisfied by
    marking no zones at all, which is precisely what this scenario did before it was given a trend
    to break.
    """
    candles = [
        bar(0, open_="122", close="122", high="123", low="120"),
        bar(1, open_="119", close="119", high="122", low="118"),
        bar(2, open_="117", close="117", high="121", low="116"),
        bar(3, open_="99", close="99", high="100", low="98"),  # origin of the impulse
        bar(4, open_="104", close="104", high="105", low="103"),
        bar(5, open_="108", close="108", high="110", low="102"),  # the impulse's own gap
        bar(6, open_="113", close="113", high="115", low="107"),
        bar(7, open_="112", close="112", high="117", low="110"),
        bar(8, open_="116", close="118", high="119", low="112"),
        bar(9, open_="124", close="124", high="125", low="120"),  # BOS
    ]
    zones = _zones_from_bullish(candles)

    assert [z.time for _, z in zones] == [_at(3), _at(7)]


def test_a_bearish_break_marks_supply_from_bearish_gaps_only() -> None:
    """The mirror: a down impulse marks supply, and the wick extends the zone *up*.

    Same shape reflected about 220, so the leg falls from 122 to a close below the 97 bottom. The
    zone is the candle before the gap, its top lifted by the impulse candle's high where that ran
    higher. A bullish gap in the same leg would point the wrong way and must be ignored.
    """
    candles = [
        bar(0, open_="98", close="98", high="100", low="97"),  # bottom 97
        bar(1, open_="101", close="101", high="102", low="98"),  # up-correction 1
        bar(2, open_="103", close="103", high="104", low="99"),  # up-correction 2 -> armed
        bar(3, open_="121", close="121", high="122", low="120"),  # impulse starts; origin high 122
        bar(4, open_="116", close="116", high="117", low="115"),
        bar(5, open_="112", close="112", high="118", low="110"),  # bearish gap: 120 > 118
        bar(6, open_="107", close="107", high="113", low="105"),  # adjacent gap, same event
        bar(7, open_="108", close="108", high="110", low="103"),  # pause
        bar(8, open_="104", close="102", high="108", low="101"),  # pause; closes under 103
        bar(9, open_="96", close="96", high="100", low="95"),  # gap + close 96 < 97 -> bearish BOS
    ]
    zones = _zones(candles)

    assert [(z.kind, z.time, z.top, z.bottom, z.primary) for _, z in zones] == [
        (ZoneKind.SUPPLY, _at(3), Decimal("122"), Decimal("120"), True),
        (ZoneKind.SUPPLY, _at(7), Decimal("110"), Decimal("103"), False),
    ]


def test_an_impulse_without_a_gap_marks_no_zone() -> None:
    """No inefficiency, no footprint: a break that leaves no gap marks nothing by this method.

    The author's fallback for this case — mark the candle of the swing that began the move — is a
    separate rule, deliberately not folded into the inefficiency marking.

    On the bullish start, so that the break on bar 4 genuinely happens and is genuinely empty
    handed. Without it nothing breaks at all, and "no zone" would be true of a scenario in which
    nothing was ever looked for.
    """
    candles = [
        bar(0, open_="99", close="99", high="100", low="95"),
        bar(1, open_="104", close="104", high="105", low="99"),  # top 105
        bar(2, open_="99", close="99", high="103", low="98"),  # correction 1
        bar(3, open_="97", close="97", high="101", low="96"),  # correction 2 -> armed
        bar(4, open_="106", close="106", high="107", low="100"),  # breaks, but overlaps throughout
    ]
    assert _breaks_from_bullish(candles) != []  # the break happened...
    assert _zones_from_bullish(candles) == []  # ...and left no footprint


# --- Zone lifecycle -----------------------------------------------------------------------------
#
# His whole rule, and it is one line of his indicator:
#
#     ob.bull ? low <= ob.topo : high >= ob.fundo
#
# The near edge is the side price must come back to — a demand zone's top, a supply zone's bottom —
# and the first wick that reaches it takes the region. By wick, not by close; once, not by degree.
#
# The zones below are ten points wide so that "reached the near edge" and "reached the far one" are
# ten points apart: a mutant that reads the wrong edge has to change an answer, not a rounding.
# Every rule here is asserted on **both** sides and with the boundary sampled from both directions,
# because an earlier version of this file asserted only that untouched zones stay alive — which any
# definition of the edge satisfies, including a reversed one.


def _demand_90_100() -> OrderBlock:
    return OrderBlock(
        kind=ZoneKind.DEMAND,
        top=Decimal("100"),
        bottom=Decimal("90"),
        time=_at(0),
        confirmed_at=_at(0),
        break_kind=StructureKind.BOS,
        primary=True,
    )


def _supply_100_110() -> OrderBlock:
    """The demand zone's mirror, for properties that must hold on both sides."""
    return OrderBlock(
        kind=ZoneKind.SUPPLY,
        top=Decimal("110"),
        bottom=Decimal("100"),
        time=_at(0),
        confirmed_at=_at(0),
        break_kind=StructureKind.BOS,
        primary=True,
    )


def _live(block: OrderBlock, candles: list[Candle]) -> TrackedZone:
    """Run a zone through the candles by hand, bypassing detection to isolate the lifecycle."""
    tracked = TrackedZone(block=block)
    for candle in candles:
        OrderBlockDetector._advance(tracked, candle)
    return tracked


def test_a_zone_is_not_spent_by_a_move_it_never_touched() -> None:
    """Distance is not mitigation. Only coming back is.

    A rally away from a demand zone says nothing about the orders resting inside it — they are
    still there, untouched, and the zone is still live however far price runs. This used to be
    false: the engine had invented a second death, `driven_off`, where a close a full zone-width
    clear counted as the region having done its job. That is not in his indicator and is gone.
    """
    tracked = _live(
        _demand_90_100(),
        [
            bar(0, open_="105", close="112", high="115", low="104"),  # a full width clear of 100
            bar(1, open_="112", close="118", high="120", low="111"),
        ],
    )
    assert not tracked.mitigated
    assert tracked.usable


def test_a_demand_zone_is_spent_by_the_first_wick_that_reaches_its_top() -> None:
    """The near edge of a demand zone is its **top**, and touching it is enough.

    100 exactly, by wick, on a bar that closes well above — no close inside, no penetration, no
    second visit. That is where the buy limit rests, so that is where the orders in the region are
    taken. Sampled from both sides of the boundary because both mutants are one character wide: a
    zone that only died at its *bottom* would keep offering a level the market has already worked,
    and `low < top` instead of `low <= top` would miss the exact touch that fills the order.
    """
    grazed = _live(_demand_90_100(), [bar(0, open_="105", close="104", high="106", low="100.01")])
    assert grazed.usable  # a hair above the edge: the limit at 100 would not have filled

    touched = _live(_demand_90_100(), [bar(0, open_="105", close="104", high="106", low="100")])
    assert touched.mitigated
    assert not touched.usable

    # And permanent: leaving and coming back does not revive it.
    assert not _live(
        _demand_90_100(),
        [
            bar(0, open_="105", close="104", high="106", low="100"),
            bar(1, open_="104", close="118", high="120", low="103"),
        ],
    ).usable


def test_a_supply_zone_is_spent_by_the_first_wick_that_reaches_its_bottom() -> None:
    """The same rule mirrored, and it is the half that had no test at all.

    A supply zone is sold at its **bottom**, so its near edge is the one underneath it and price
    reaches it from below. Both comparisons in `_advance` are separate lines of code; the demand
    one was covered three times over and this one zero times, which let the sell side read the far
    edge — an order left resting at a level price had already traded through.
    """
    grazed = _live(_supply_100_110(), [bar(0, open_="95", close="96", high="99.99", low="94")])
    assert grazed.usable  # a hair below the edge: the limit at 100 would not have filled

    touched = _live(_supply_100_110(), [bar(0, open_="95", close="96", high="100", low="94")])
    assert touched.mitigated
    assert not touched.usable

    assert not _live(
        _supply_100_110(),
        [
            bar(0, open_="95", close="96", high="100", low="94"),
            bar(1, open_="96", close="82", high="97", low="80"),
        ],
    ).usable


def _followed(kind: FVGKind, marking: Candle) -> _Region:
    """A region under observation, built by hand to isolate the touch rule from detection."""
    return _Region(index=0, kind=kind, marking=marking)


def test_a_followed_region_dies_at_its_entry_edge_on_either_side() -> None:
    """`_Region.touched_by` is the same rule as `_advance`, on the *other* half of the lifetime.

    A region has two lives: it is **followed** from the bar its gap completes, by anyone or no
    one, and it is **offered** when a break reveals it. `touched_by` governs the first and
    `_advance` the second, and they are separate code — a region can therefore be taken long
    before any setup hears about it. That is the whole point of the rule, and it is what the
    detector used to get wrong: 49% of regions were already spent when they were first offered.

    Asserted positively here because the only test that used to reach this method asserted the
    *negative* twice — that two named bars could not take a region — which is satisfied by every
    definition of the entry edge, including one with the sides swapped. A test that only watches
    a machine stay silent proves nothing until the machine has been given a reason to speak.
    """
    marking = bar(0, open_="95", close="99", high="100", low="90")  # the region is [90, 100]

    demand = _followed(FVGKind.BULLISH, marking)
    assert not demand.touched_by(bar(1, open_="105", close="104", high="106", low="100.01"))
    assert demand.touched_by(bar(2, open_="105", close="104", high="106", low="100"))
    assert demand.near_edge == Decimal("100")

    supply = _followed(FVGKind.BEARISH, marking)
    assert not supply.touched_by(bar(1, open_="85", close="86", high="89.99", low="84"))
    assert supply.touched_by(bar(2, open_="85", close="86", high="90", low="84"))
    assert supply.near_edge == Decimal("90")


def test_a_region_cannot_be_taken_by_the_bar_that_created_it() -> None:
    """The gap's own geometry rules that out, so nothing in the code has to.

    This replaces a test for a rule that no longer exists. The detector used to hold a region
    "clean" at birth on purpose — the leg that revealed it was never replayed against it — because
    replaying it marked almost every region spent immediately. That was a workaround for a
    mitigation rule the engine had invented; his is narrower and needs no help.

    On the bar a gap completes, his condition already puts price clear of the region: a bullish gap
    requires `low > high[2]`, and the region's top *is* `high[2]`, so the touch test `low <= top`
    is false there by construction. The impulse bar between them is never tested at all, because
    the region does not exist until the gap closes.

    Asserted on the two bars that could do it — the gap bar and the impulse before it — rather
    than on the flag alone, so this fails if the region ever starts being followed too early.
    """
    detector = OrderBlockDetector()
    for candle in [*BULLISH_START, *_OB_IMPULSE[:6]]:
        detector.update(candle, None)

    # By its marking bar: other regions are alive here, including one the prefix left. That they
    # coexist is the point — regions are followed from their own gaps, independently.
    marking = _OB_IMPULSE[3]
    region = next(r for r in detector._regions if r.marking.time == marking.time)
    assert (region.top, region.bottom) == (marking.high, marking.low)
    assert not region.mitigated  # nothing so far has been able to take it

    # And neither of the two bars that made it could have: the impulse is never tested, and the
    # gap bar is clear of the region by the gap condition itself.
    assert not region.touched_by(_OB_IMPULSE[4])
    assert not region.touched_by(_OB_IMPULSE[5])


def _visited(dip_low: str) -> list[Candle]:
    """One impulse leg, twice: the only difference is how deep bar 6 pulls back.

    The region is marked on bar 3 at [98, 100] and born on bar 5, when its gap completes. Bar 6
    is the pullback — a low of 101 leaves it alone, a low of 100 touches its top exactly — and
    bar 6 is *also* the marking candle of a second gap, so whichever way it goes it leaves a
    region of its own behind. Bar 9 closes through 123 and breaks structure over both.
    """
    return [
        bar(0, open_="122", close="122", high="123", low="120"),  # top 123
        bar(1, open_="119", close="119", high="122", low="118"),  # correction 1
        bar(2, open_="117", close="117", high="121", low="116"),  # correction 2 -> armed
        bar(3, open_="99", close="99", high="100", low="98"),  # marks the region [98, 100]
        bar(4, open_="104", close="104", high="105", low="103"),
        bar(5, open_="108", close="108", high="110", low="102"),  # gap: 100 < 102, close[1] = 104
        bar(6, open_="106", close="106", high="107", low=dip_low),  # the visit, or the near miss
        bar(7, open_="110", close="110", high="112", low="106"),
        bar(8, open_="116", close="116", high="118", low="112"),
        bar(9, open_="124", close="124", high="125", low="120"),  # close 124 > 123 -> BOS
    ]


def test_a_region_taken_before_the_break_is_never_offered() -> None:
    """The rule this whole change exists for: a break reveals only what is still standing.

    A region is born at its gap and price starts working it immediately, but nothing *asks* about
    it until a break comes along — a median of 16 bars later, 282 at the worst. The engine used to
    create the region at the break and so knew nothing of those bars, which meant **49% of the
    regions it offered were already spent**. Measured, and it is the bug behind the worked example
    in `docs/referencia/indicador-regioes-order-block.md`: a primary marked on 14/08, first touched
    four hours later, offered fresh to a CHoCH on 21/08 and filled on 28/08 for -1.73R.

    Two scenarios that differ by one point of one low, so the machine has every chance to speak:

    * bar 6 bottoms at 101 — the region survives and is offered as the primary;
    * bar 6 bottoms at 100 — its top exactly — and it is **gone**, with the region bar 6 marked on
      its way through promoted to primary in its place.

    The promotion is what makes this sharp. A regression does not merely add a zone back to the
    list, it changes which region the setup is handed first, and `allow_secondary: false` means
    the primary is the only one most runs ever see.

    Pinned as well: the break reports the **same** `origin_time` in both, so the vanishing cannot
    be the impulse leg having moved out from under the region. Only mitigation is left.
    """
    survived = _zones_from_bullish(_visited("101"))
    assert [(z.bottom, z.top, z.primary) for _, z in survived] == [
        (Decimal("98"), Decimal("100"), True),
        (Decimal("101"), Decimal("107"), False),
    ]

    taken = _zones_from_bullish(_visited("100"))
    assert [(z.bottom, z.top, z.primary) for _, z in taken] == [
        (Decimal("100"), Decimal("107"), True),  # the survivor of bar 6, promoted
    ]

    origins = [
        events[-1][1].origin_time
        for events in (_breaks_from_bullish(_visited(low)) for low in ("101", "100"))
    ]
    assert origins[0] == origins[1] == _at(3)  # the leg is identical; only the touch differs


def test_a_choch_marks_its_zone_and_says_so() -> None:
    """Zones come from changes of character too, and carry which kind of break revealed them.

    A CHoCH reverses the trend, so the region its impulse leaves is the one the CHoCH setup waits
    for price to return to — and telling it apart from a BOS zone is what lets a setup pick.
    """
    candles = [
        bar(0, open_="99", close="99", high="100", low="95"),
        bar(1, open_="104", close="104", high="105", low="99"),  # top 105
        bar(2, open_="99", close="99", high="103", low="98"),  # correction 1
        bar(3, open_="97", close="97", high="101", low="96"),  # correction 2 -> armed
        bar(4, open_="106", close="106", high="107", low="103"),  # BOS up, anchor at 96
        bar(5, open_="106", close="105", high="107", low="104"),
        bar(6, open_="105", close="99", high="105", low="98"),  # falling
        bar(7, open_="99", close="94", high="99", low="93"),  # closes 94 < 96 -> CHoCH down
    ]
    zones = _zones_from_bullish(candles)

    assert [(z.kind, z.break_kind) for _, z in zones] == [(ZoneKind.SUPPLY, StructureKind.CHOCH)]


# Prices that straddle every boundary a zone's rules can turn on: a full width below, the bottom
# edge and either side of it, the middle, the top edge and either side, one width above, and well
# clear in both directions.


def _grid_candle(index: int, prices: tuple[str, str, str, str]) -> Candle:
    """A valid candle from four sampled prices — high and low are their extremes, so the body
    always fits inside the range whatever was drawn."""
    values = [Decimal(price) for price in prices]
    return Candle(
        time=_at(index),
        open=values[0],
        close=values[3],
        high=max(values),
        low=min(values),
    )


def test_the_stamp_is_the_bar_that_took_the_zone_not_the_last_one_to_visit() -> None:
    """⚠️ The whole subtlety of carrying a time alongside the boolean.

    `mitigated` is folded forward with `or`, so once true it stays true through every later bar —
    including the many that also reach the edge, because price ranges around a level it has just
    worked. A stamp written on the same terms ("if it is reached, record this bar") would keep
    moving to the most recent visit.

    Nothing about the run's decisions would change: `usable` only reads the boolean. What changes
    is the picture. The chart draws a region from the bar that marked it to the bar that took it,
    so the rectangle's length is *how long the region stood* — and a stamp that crept forward
    would draw a zone as having survived until the last time price happened to be there, which
    across a range is many bars past the touch that actually killed it. A longer rectangle is not
    an obviously wrong one.

    Three visits here, and only the first may be recorded.
    """
    tracked = _live(
        _demand_90_100(),
        [
            bar(0, open_="105", close="104", high="106", low="100"),  # takes it, at the edge
            bar(1, open_="104", close="103", high="105", low="98"),  # deep inside, later
            bar(2, open_="103", close="106", high="107", low="95"),  # deeper still, later again
        ],
    )

    assert tracked.mitigated
    assert tracked.mitigated_at == _at(0)


def test_a_zone_nothing_came_back_to_carries_no_time_at_all() -> None:
    """`None` is "still standing", not "unknown" — and it is what tells a chart to extend the
    rectangle to its own right edge rather than closing it at some invented bar."""
    tracked = _live(_demand_90_100(), [bar(0, open_="105", close="112", high="115", low="104")])

    assert not tracked.mitigated
    assert tracked.mitigated_at is None


def test_the_stamp_and_the_flag_are_set_by_the_same_bar() -> None:
    """They are two readings of one event, and a scenario where they disagree is a bug in one of
    them. Sampled on the exact boundary, where a `<` against a `<=` would separate the pair."""
    grazed = _live(_demand_90_100(), [bar(0, open_="105", close="104", high="106", low="100.01")])
    assert (grazed.mitigated, grazed.mitigated_at) == (False, None)

    touched = _live(_demand_90_100(), [bar(0, open_="105", close="104", high="106", low="100")])
    assert touched.mitigated
    assert touched.mitigated_at == _at(0)

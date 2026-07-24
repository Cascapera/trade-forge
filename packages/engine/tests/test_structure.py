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
    _WedgeTracker,
)
from tradeforge_engine.testing import HOUR, START, bar


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
    candle of its window, zoned exactly by their wicks and timed to the third bar (soundness); and
    where it reports nothing, neither inequality held (completeness) — so a flipped bound, a
    non-strict compare, or a stray filter on the middle candle would fail one half or the other."""
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
        if gap is None:
            # completeness: a silent bar must hide no gap.
            assert not first.high < third.low
            assert not first.low > third.high
            continue
        assert gap.time == third.time
        assert gap.top > gap.bottom
        if gap.kind is FVGKind.BULLISH:
            assert first.high < third.low
            assert gap.bottom == first.high
            assert gap.top == third.low
        else:
            assert first.low > third.high
            assert gap.top == first.low
            assert gap.bottom == third.high


# --------------------------------------------------------------------------- #
# Market structure — BOS and CHoCH                                              #
# --------------------------------------------------------------------------- #


def _breaks(candles: list[Candle]) -> list[tuple[int, StructureBreak]]:
    structure = MarketStructure()
    found: list[tuple[int, StructureBreak]] = []
    for index, candle in enumerate(candles):
        event = structure.update(candle)
        if event is not None:
            found.append((index, event))
    return found


def _bos(trend: Trend, level: str, at: int, *, origin: str, origin_at: int) -> StructureBreak:
    """A bullish/bearish BOS confirmed on bar `at`, whose impulse started at `origin` on
    `origin_at` — the extreme the move came from, which is also the next opposite CHoCH anchor."""
    return StructureBreak(
        kind=StructureKind.BOS,
        trend=trend,
        level=Decimal(level),
        time=_at(at),
        origin=Decimal(origin),
        origin_time=_at(origin_at),
    )


def _choch(trend: Trend, level: str, at: int, *, origin: str, origin_at: int) -> StructureBreak:
    """A CHoCH confirmed on bar `at`; `origin` is the extreme the reversing move began from."""
    return StructureBreak(
        kind=StructureKind.CHOCH,
        trend=trend,
        level=Decimal(level),
        time=_at(at),
        origin=Decimal(origin),
        origin_time=_at(origin_at),
    )


# The author's hand-worked example: a bullish BOS bootstraps the trend, then a bearish CHoCH turns
# it. high, low, close per bar (open = close for simplicity).
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
    defended). Exactly the two events the method's author marked."""
    assert _breaks(_STRUCTURE_GOLDEN) == [
        (5, _bos(Trend.BULLISH, "105", 5, origin="96", origin_at=3)),
        (8, _choch(Trend.BEARISH, "96", 8, origin="107", origin_at=5)),
    ]


def test_trend_is_none_until_the_first_bos() -> None:
    structure = MarketStructure()
    for candle in _STRUCTURE_GOLDEN[:5]:
        structure.update(candle)
        assert structure.trend is None
    structure.update(_STRUCTURE_GOLDEN[5])
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
        (5, _bos(Trend.BEARISH, "90", 5, origin="103", origin_at=3)),
        (8, _choch(Trend.BULLISH, "103", 8, origin="88", origin_at=5)),
    ]


def test_one_correction_bar_does_not_arm_a_bos() -> None:
    """Two consecutive correction bars are required. With only one, the top is unarmed and a close
    above it is not a break of structure."""
    candles = [
        bar(0, open_="99", close="99", high="100", low="95"),
        bar(1, open_="104", close="104", high="105", low="99"),  # top 105
        bar(2, open_="99", close="99", high="103", low="98"),  # a single correction bar
        bar(3, open_="106", close="106", high="107", low="102"),  # closes above 105 but unarmed
    ]
    assert _breaks(candles) == []


def test_a_wick_through_the_top_without_a_close_is_no_bos() -> None:
    """The break is by close, not by pierce: a bar whose high tags the top but whose close stays
    below it does not confirm a BOS (here it simply lifts the top)."""
    candles = [
        bar(0, open_="99", close="99", high="100", low="95"),
        bar(1, open_="104", close="104", high="105", low="99"),  # top 105
        bar(2, open_="99", close="99", high="103", low="98"),  # correction 1
        bar(3, open_="97", close="97", high="101", low="96"),  # correction 2 -> armed
        bar(4, open_="103", close="104", high="107", low="100"),  # high 107 > 105, close 104 < 105
    ]
    assert _breaks(candles) == []


def test_a_non_correction_bar_becomes_the_next_correction_reference() -> None:
    """The author's rule, pinned: after the top, a bar that is not a correction becomes the
    reference, and correction is measured against it, and so on. Bar 2 has a higher low than the
    105 top (not a correction), so it is the reference; bars 3 and 4 step down from it and arm the
    top — a BOS — even though neither dipped below the top's own low of 99."""
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
    assert _breaks(candles) == [
        (5, _bos(Trend.BULLISH, "105", 5, origin="99", origin_at=1)),
    ]


def test_a_close_exactly_at_the_top_is_not_a_break() -> None:
    """Strict inequality for structure too: a close landing exactly on the top does not confirm."""
    candles = [
        bar(0, open_="99", close="99", high="100", low="95"),
        bar(1, open_="104", close="104", high="105", low="99"),  # top 105
        bar(2, open_="99", close="99", high="103", low="98"),  # correction 1
        bar(3, open_="97", close="97", high="101", low="96"),  # correction 2 -> armed
        bar(4, open_="104", close="105", high="105", low="100"),  # close 105 == top, not above
    ]
    assert _breaks(candles) == []


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
    assert _breaks(candles) == [
        (5, _bos(Trend.BULLISH, "105", 5, origin="96", origin_at=3)),
        (8, _bos(Trend.BULLISH, "107", 8, origin="99", origin_at=7)),
        (10, _choch(Trend.BEARISH, "99", 10, origin="109", origin_at=8)),
    ]


def test_a_choch_can_flip_back() -> None:
    """After the bearish CHoCH the bias is bearish and the next CHoCH points up at the high the
    failed up-move made (107). A close back above 107 flips it bullish again."""
    extended = [
        *_STRUCTURE_GOLDEN,
        bar(9, open_="96", close="96", high="99", low="93"),  # bearish leg makes a new low
        bar(10, open_="108", close="108", high="109", low="100"),  # close 108 > 107 -> CHoCH up
    ]
    assert _breaks(extended)[-1] == (
        10,
        _choch(Trend.BULLISH, "107", 10, origin="93", origin_at=9),
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
# The author's validated example, with three bars in front to set up the break. Bars 0-2 leave a
# top at 123 and two corrections that arm it; bars 3-9 are the impulse that breaks it, and inside
# that impulse the gaps come in two events separated by a pause:
#
#   bar 3  high 100  low  98  ┐
#   bar 4  high 105  low 103  │ gap A: 100 < 102  (confirms on bar 5)
#   bar 5  high 110  low 102  ┘
#   bar 6  high 115  low 107    gap B: 105 < 107  (confirms on bar 6) -- adjacent to A, same event
#   bar 7  high 117  low 110    no gap: 110 < 110 is not strict      -- THE PAUSE
#   bar 8  high 118  low 112    no gap
#   bar 9  high 125  low 120    gap C: 117 < 120  (confirms on bar 9) -- a second event
#
# So two zones, not three: A and B are one continuous push.
_OB_IMPULSE = [
    bar(0, open_="122", close="122", high="123", low="120"),  # top 123
    bar(1, open_="119", close="119", high="122", low="118"),  # correction 1
    bar(2, open_="117", close="117", high="121", low="116"),  # correction 2 -> armed
    bar(3, open_="99", close="99", high="100", low="98"),  # impulse starts; origin low 98
    bar(4, open_="104", close="104", high="105", low="103"),
    bar(5, open_="108", close="108", high="110", low="102"),  # gap A
    bar(6, open_="113", close="113", high="115", low="107"),  # gap B
    bar(7, open_="112", close="112", high="117", low="110"),  # pause
    bar(8, open_="116", close="116", high="118", low="112"),  # pause
    bar(9, open_="124", close="124", high="125", low="120"),  # gap C, and close 124 > 123 -> BOS
]


def _zones(candles: list[Candle]) -> list[tuple[int, OrderBlock]]:
    """Drive structure and order blocks together the way a strategy would, one bar at a time."""
    structure, blocks = MarketStructure(), OrderBlockDetector()
    found: list[tuple[int, OrderBlock]] = []
    for index, candle in enumerate(candles):
        break_ = structure.update(candle)
        found.extend((index, zone) for zone in blocks.update(candle, break_))
    return found


def test_order_block_golden_one_impulse_two_gap_events_two_zones() -> None:
    """The author's example: an impulse gapping twice, with a pause between, marks exactly 2 zones.

    Both zones are the candle *before* their gap event, and both are widened by the gap candle's
    wick only where that wick ran deeper — here neither did, so each keeps its own low. The primary
    is the first event; the secondary is the one after the pause. Gaps A and B are adjacent bars,
    one continuous push, so they share a zone: without that rule this leg would mark three.
    """
    zones = _zones(_OB_IMPULSE)

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
    index, zone = _zones(_OB_IMPULSE)[0]
    assert zone.time == _at(3)
    assert zone.confirmed_at == _at(9)
    assert index == 9
    assert zone.time < zone.confirmed_at


def test_the_gap_candles_wick_extends_the_demand_zone_down() -> None:
    """When the impulse candle dips below the marking candle, that wick joins the zone.

    The author is explicit that the wick is part of where the institutions worked, so bar 5's low
    of 96 pulls the zone down past bar 4's own 98 — the difference between a stop that survives the
    retest and one that does not.

    Bar 3 has to dig deeper than that wick for the case to exist at all. The impulse leg starts at
    its lowest low, so if the gap candle were the deepest bar of the move it *would* be the origin,
    and the gap before it would fall outside the leg entirely and mark nothing.
    """
    candles = [
        *_OB_IMPULSE[:3],
        # Deepest bar: the leg's origin. Its high of 96 ties bar 5's low, so it opens no gap of
        # its own — the strict `<` keeps this run starting at bar 4, where the case needs it.
        bar(3, open_="92", close="92", high="96", low="90"),
        bar(4, open_="99", close="99", high="100", low="98"),  # marking candle
        bar(5, open_="104", close="104", high="105", low="96"),  # gap candle, wicks below the 98
        bar(6, open_="108", close="108", high="110", low="102"),  # gap: 100 < 102
        *_OB_IMPULSE[7:],
    ]
    _, primary = _zones(candles)[0]

    assert primary.time == _at(4)
    assert primary.top == Decimal("100")  # still the marking candle's high
    assert primary.bottom == Decimal("96")  # extended down by the impulse candle's wick


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
    zones = _zones(candles)

    assert [(z.time, z.primary) for _, z in zones] == [(_at(3), True)]


def test_a_gap_outside_the_impulse_leg_marks_nothing() -> None:
    """Only the leg that broke structure leaves a footprint worth trading.

    The gap here forms during the correction, before the impulse's origin, so it is not the move
    that broke anything. The impulse itself gaps too, and only that one marks a zone.
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
        bar(8, open_="116", close="116", high="118", low="112"),
        bar(9, open_="124", close="124", high="125", low="120"),  # BOS
    ]
    zones = _zones(candles)

    # Every zone reported sits at or after the break's own origin bar, never before it.
    assert all(z.time >= _at(3) for _, z in zones)


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
        bar(8, open_="104", close="104", high="108", low="102"),  # pause
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
    """
    candles = [
        bar(0, open_="99", close="99", high="100", low="95"),
        bar(1, open_="104", close="104", high="105", low="99"),  # top 105
        bar(2, open_="99", close="99", high="103", low="98"),  # correction 1
        bar(3, open_="97", close="97", high="101", low="96"),  # correction 2 -> armed
        bar(4, open_="106", close="106", high="107", low="100"),  # breaks, but overlaps throughout
    ]
    assert _zones(candles) == []


# --- Zone lifecycle -----------------------------------------------------------------------------
#
# The author's own worked example, kept literal: a demand zone from 90 to 100, so ten points wide.
# Price arrives from 105, dips to 98 (a touch), and the zone is spent only once price is driven a
# full width clear of it — past 100 + 10 = 110.


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


def test_zone_lifecycle_golden_touched_then_driven_off_by_one_width() -> None:
    """The author's example: [90, 100] touched at 98, mitigated only once price clears 110.

    The zone is not spent by being touched — it is spent by *working*. Price coming back to 105 or
    even 109 leaves it live, because nothing has proved the orders resting there were filled. Once
    price is pushed a full zone-width beyond it, the move those orders fund is underway and the
    zone has done its job.
    """
    block = _demand_90_100()
    approach = [
        bar(0, open_="105", close="104", high="106", low="103"),  # above the zone, no touch yet
        bar(1, open_="104", close="99", high="104", low="98"),  # dips in: touched
        bar(2, open_="99", close="108", high="109", low="99"),  # rallies to 109 — not yet clear
    ]
    tracked = _live(block, approach)
    # Asserted as a tuple so each check reads against the whole state, not one flag at a time.
    assert (tracked.touched, tracked.mitigated, tracked.usable) == (True, False, True)

    OrderBlockDetector._advance(
        tracked,
        bar(3, open_="108", close="111", high="112", low="108"),  # closes 111, past 110
    )
    # Spent the healthy way: it worked, so it is used up but was never traded through.
    assert (tracked.mitigated, tracked.usable, tracked.flipped) == (True, False, False)


def test_a_zone_is_not_spent_by_a_move_it_never_touched() -> None:
    """Both halves are required: price must reach the zone before running away can spend it.

    A rally straight past 110 that never came down to 100 says nothing about the orders resting at
    90 — they are still there, untouched, and the zone is still live.
    """
    tracked = _live(
        _demand_90_100(),
        [
            bar(0, open_="105", close="112", high="115", low="104"),  # clears 110 without touching
            bar(1, open_="112", close="118", high="120", low="111"),
        ],
    )
    assert not tracked.touched
    assert not tracked.mitigated
    assert tracked.usable


def test_exactly_one_width_away_is_not_yet_clear_of_the_zone() -> None:
    """Strict again: closing at 110 exactly is one width away, not more than one.

    And the wick is not what decides: the second bar here runs to 118, far past a full width, yet
    ends the bar at 110. The zone stands.
    """
    touched_then_110 = [
        bar(0, open_="104", close="99", high="104", low="98"),  # touch
        bar(1, open_="99", close="110", high="118", low="99"),  # closes exactly at 100 + 10
    ]
    assert not _live(_demand_90_100(), touched_then_110).mitigated


def test_a_wick_through_the_zone_flips_it_but_leaves_it_usable() -> None:
    """The author's rule: pierced but not closed beyond, the zone is flipped and still stands.

    Price traded below 90 and came back — the level was defended. The flip mark is what makes this
    zone interesting to the flip setup later, but it has not stopped being demand.
    """
    tracked = _live(
        _demand_90_100(),
        [bar(0, open_="95", close="94", high="96", low="88")],  # low 88 < 90, close 94 > 90
    )
    assert tracked.flipped
    assert not tracked.mitigated
    assert tracked.usable


def test_a_close_beyond_the_zone_flips_and_spends_it() -> None:
    """Closing through is the failed ending: the zone is both flipped and mitigated.

    Nothing is created here — no new supply zone is born. The two marks are simply left on the
    zone, and it is that pair the flip setup will look for when it wants to sell against the trend
    at a demand zone the market has broken.
    """
    tracked = _live(
        _demand_90_100(),
        [bar(0, open_="95", close="88", high="96", low="87")],  # closes below 90
    )
    assert tracked.flipped
    assert tracked.mitigated
    assert not tracked.usable


def test_the_supply_mirror_spends_downward() -> None:
    """A supply zone [100, 110] is touched from below and spent by a drop past 100 - 10 = 90."""
    supply = OrderBlock(
        kind=ZoneKind.SUPPLY,
        top=Decimal("110"),
        bottom=Decimal("100"),
        time=_at(0),
        confirmed_at=_at(0),
        break_kind=StructureKind.CHOCH,
        primary=True,
    )
    tracked = _live(
        supply,
        [
            bar(0, open_="95", close="98", high="102", low="94"),  # reaches up into the zone
            bar(1, open_="98", close="92", high="99", low="91"),  # falling, not yet clear
        ],
    )
    assert (tracked.touched, tracked.mitigated) == (True, False)

    OrderBlockDetector._advance(tracked, bar(2, open_="92", close="89", high="93", low="88"))
    assert tracked.mitigated  # closes 89, past 90

    # Pierced above the top but closed back under it: flipped, and still supply.
    wicked = _live(supply, [bar(0, open_="105", close="108", high="113", low="104")])
    assert (wicked.flipped, wicked.mitigated, wicked.usable) == (True, False, True)

    # Closed above the top: flipped and spent.
    closed_through = _live(supply, [bar(0, open_="105", close="112", high="113", low="104")])
    assert (closed_through.flipped, closed_through.mitigated) == (True, True)


def test_marks_are_permanent_once_set() -> None:
    """A zone never heals: later candles cannot un-flip or un-spend it.

    Without this a zone broken on Monday would quietly become tradeable again on Tuesday, and the
    flip setup would lose the very history it exists to trade.
    """
    tracked = _live(
        _demand_90_100(),
        [
            bar(0, open_="95", close="88", high="96", low="87"),  # closes through: flipped + spent
            bar(1, open_="88", close="95", high="96", low="88"),  # back inside
            bar(2, open_="95", close="99", high="100", low="94"),  # quietly trading in the zone
        ],
    )
    assert tracked.flipped
    assert tracked.mitigated


def test_a_zone_is_born_clean_and_not_touched_by_the_bar_that_revealed_it() -> None:
    """The breaking bar must not count against the zone it just revealed.

    The bar here is an outside bar: it closes at 124 to confirm the BOS, but its low of 99 reaches
    down into the primary zone [98, 100] and its high of 125 is more than a full width clear of it.
    If zones were marked before being advanced, this one would be born already touched *and*
    already mitigated — dead on arrival, on information nobody had until this very bar closed.

    The low of 99 also leaves the leg's origin at 98 on bar 3, so the zone itself is unchanged;
    only the order of the two halves of `update` decides the outcome. Reverse them and this fails.

    Reaching down to 99 does cost the leg its second gap — 117 is no longer below the breaking
    bar's low — so this leg marks the primary zone alone, which is the one the case needs.
    """
    reveal = bar(9, open_="124", close="124", high="125", low="99")
    structure, blocks = MarketStructure(), OrderBlockDetector()
    for candle in [*_OB_IMPULSE[:9], reveal]:
        blocks.update(candle, structure.update(candle))

    (zone,) = blocks.zones
    assert (zone.block.time, zone.block.top, zone.block.bottom) == (
        _at(3),
        Decimal("100"),
        Decimal("98"),
    )
    assert (zone.touched, zone.mitigated, zone.usable) == (False, False, True)


def test_a_news_spike_out_of_the_zone_does_not_spend_it() -> None:
    """The author's case: a bar can trade far outside a zone and still close inside it.

    News drives price to 112 — past 110, a full width clear of [90, 100] — and it is back at 94 by
    the close. Judged on the wick the zone would be spent, and because a mitigated zone can never
    flip, the break on the next bar would silently stop being a flip. Judged on the close, which is
    how every other decision in this module is made, the market never left and the flip stands.
    """
    tracked = _live(
        _demand_90_100(),
        [
            bar(0, open_="105", close="96", high="106", low="95"),  # falls in, closes inside
            bar(1, open_="96", close="94", high="112", low="93"),  # spike to 112, closes at 94
        ],
    )
    assert (tracked.touched, tracked.departed, tracked.mitigated) == (True, False, False)

    OrderBlockDetector._advance(tracked, bar(2, open_="94", close="88", high="95", low="88"))
    assert (tracked.flipped, tracked.mitigated) == (True, True)


def test_a_zone_price_left_and_came_back_to_cannot_flip() -> None:
    """A flip has to arrive and take the zone out in one drive, not on a second visit.

    Price touches at 95, then *closes* at 106 — clear of the zone, so it backed away. The later
    break through 90 is no longer the abrupt move the setup is built on, so it marks no flip, and
    the zone is left alone.
    """
    tracked = _live(
        _demand_90_100(),
        [
            bar(0, open_="105", close="96", high="106", low="95"),  # touch, closes inside
            bar(1, open_="96", close="106", high="107", low="96"),  # closes clear: departed
            bar(2, open_="106", close="88", high="106", low="88"),  # breaks through, but too late
        ],
    )
    # Not a flip — but the break still spends the zone. Asserting only the flip marks here would
    # miss a zone the market has closed straight through still reporting itself as tradeable.
    assert (tracked.departed, tracked.flipped, tracked.mitigated, tracked.usable) == (
        True,
        False,
        True,
        False,
    )


def test_two_falling_bars_are_still_one_drive() -> None:
    """Breaking over two bars without a close back out of the zone is still abrupt.

    The first bar closes lower inside the zone and the second carries straight on through. Nothing
    stepped back, so nothing interrupted the drive.
    """
    tracked = _live(
        _demand_90_100(),
        [
            bar(0, open_="105", close="96", high="106", low="95"),
            bar(1, open_="96", close="88", high="97", low="88"),
        ],
    )
    assert (tracked.departed, tracked.flipped) == (False, True)


def test_a_mitigated_zone_can_never_flip_again() -> None:
    """Once spent, the zone is gone — a later break through it is not a flip.

    The author was explicit: a mitigated region no longer exists, so there is nobody left in it to
    trap. This is the rule that stops old, worked-through levels from generating flip signals
    forever.
    """
    tracked = _live(
        _demand_90_100(),
        [
            bar(0, open_="105", close="99", high="106", low="98"),  # touch
            bar(1, open_="99", close="112", high="113", low="99"),  # closes past 110: spent
            bar(2, open_="112", close="88", high="112", low="88"),  # straight through, no flip
        ],
    )
    assert (tracked.mitigated, tracked.flipped) == (True, False)


def test_a_close_exactly_on_the_edge_neither_flips_nor_spends() -> None:
    """Sitting on the boundary is not beyond it — the same strictness used everywhere here."""
    on_the_bottom = _live(
        _demand_90_100(),
        [bar(0, open_="95", close="90", high="96", low="90")],  # low and close both exactly 90
    )
    assert (on_the_bottom.flipped, on_the_bottom.mitigated) == (False, False)


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
    zones = _zones(candles)

    assert [(z.kind, z.break_kind) for _, z in zones] == [(ZoneKind.SUPPLY, StructureKind.CHOCH)]


def test_a_zero_width_zone_is_spent_by_any_close_past_it() -> None:
    """A marking candle with no range leaves a zone of width zero, and one width away is nothing.

    Documented rather than special-cased: the rule reads the same, it just degenerates. A doji
    narrow enough to price at a single level gives a level, not an area, and any close beyond it
    counts as being driven off.
    """
    point = OrderBlock(
        kind=ZoneKind.DEMAND,
        top=Decimal("100"),
        bottom=Decimal("100"),
        time=_at(0),
        confirmed_at=_at(0),
        break_kind=StructureKind.BOS,
        primary=True,
    )
    tracked = _live(point, [bar(0, open_="99", close="101", high="102", low="99")])
    assert (tracked.touched, tracked.mitigated) == (True, True)


def test_a_close_through_the_zone_always_spends_it_even_after_price_left() -> None:
    """A level the market has closed beyond is gone, whatever the zone's history.

    Price touches the demand, backs away above it, then reverses and collapses thirty points
    below. That is the most ordinary way a demand zone dies. The flip marks do not apply — the
    drive was not abrupt — but the zone must not survive as something a setup would still buy.

    Worth stating why this needs saying: the healthy mitigation test only ever looks *upward* for
    a demand zone (`close > top + size`). If a departed zone could not be spent by a close through
    the bottom, nothing else would ever spend it, and it would report itself tradeable forever.
    """
    tracked = _live(
        _demand_90_100(),
        [
            bar(0, open_="102", close="99", high="103", low="98"),  # touch
            bar(1, open_="99", close="105", high="106", low="99"),  # closes clear: departed
            bar(2, open_="104", close="82", high="104", low="80"),  # closes through the bottom
            bar(3, open_="82", close="70", high="83", low="68"),  # and keeps going
        ],
    )
    assert (tracked.departed, tracked.flipped) == (True, False)
    assert (tracked.mitigated, tracked.usable) == (True, False)

    supply = OrderBlock(
        kind=ZoneKind.SUPPLY,
        top=Decimal("110"),
        bottom=Decimal("100"),
        time=_at(0),
        confirmed_at=_at(0),
        break_kind=StructureKind.BOS,
        primary=True,
    )
    mirrored = _live(
        supply,
        [
            bar(0, open_="98", close="101", high="102", low="97"),  # touch from below
            bar(1, open_="101", close="95", high="101", low="94"),  # closes clear: departed
            bar(2, open_="95", close="125", high="126", low="95"),  # closes through the top
        ],
    )
    assert (mirrored.mitigated, mirrored.usable) == (True, False)


def test_touching_and_leaving_within_one_bar_still_counts_as_leaving() -> None:
    """The same price history must read the same whether it lands in one candle or two.

    A bar that dips to 98 and closes at 105 touched the zone and left it, exactly as two bars
    doing the same would. If `departed` were decided on the touch state from *before* this bar,
    the one-bar version would still look virgin and the later break would be marked a flip — so
    the very same strategy would flip on M5 and not on M15. Timeframe is not supposed to change
    what happened.
    """
    one_bar = _live(
        _demand_90_100(),
        [
            bar(0, open_="102", close="105", high="106", low="98"),  # touches and leaves at once
            bar(1, open_="105", close="88", high="105", low="85"),  # breaks through
        ],
    )
    two_bars = _live(
        _demand_90_100(),
        [
            bar(0, open_="102", close="99", high="103", low="98"),  # touches
            bar(1, open_="99", close="105", high="106", low="99"),  # leaves
            bar(2, open_="105", close="88", high="105", low="85"),  # breaks through
        ],
    )
    state = (True, False, True, False)  # departed, flipped, mitigated, usable
    assert (one_bar.departed, one_bar.flipped, one_bar.mitigated, one_bar.usable) == state
    assert (two_bars.departed, two_bars.flipped, two_bars.mitigated, two_bars.usable) == state


# Prices that straddle every boundary a zone's rules can turn on: a full width below, the bottom
# edge and either side of it, the middle, the top edge and either side, one width above, and well
# clear in both directions.
_ZONE_GRID = ["70", "85", "89", "90", "95", "100", "101", "110", "111", "125"]


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


_GRID_SERIES = st.lists(st.tuples(*[st.sampled_from(_ZONE_GRID)] * 4), min_size=1, max_size=8)


@given(prices=_GRID_SERIES, demand=st.booleans())
def test_a_close_beyond_the_far_edge_always_spends_the_zone(
    prices: list[tuple[str, str, str, str]], demand: bool
) -> None:
    """Over random series: once any bar closes past the far side, the zone is spent. Always.

    This is the property that would have caught the real bug here. A zone the market had left and
    then closed straight through kept reporting itself tradeable — forever, because the healthy
    mitigation test only ever looks the other way. The goldens missed it because they asserted the
    marks the rule was *about* (flipped, departed) and not the one that had quietly gone wrong.
    A property does not get to choose what it looks at.
    """
    block = _demand_90_100() if demand else _supply_100_110()
    tracked = TrackedZone(block=block)
    for index, sample in enumerate(prices):
        candle = _grid_candle(index, sample)
        OrderBlockDetector._advance(tracked, candle)
        beyond = candle.close < block.bottom if demand else candle.close > block.top
        if beyond:
            assert tracked.mitigated


@given(prices=_GRID_SERIES, demand=st.booleans())
def test_zone_marks_only_ever_turn_on(
    prices: list[tuple[str, str, str, str]], demand: bool
) -> None:
    """Every mark is permanent, and a zone that stopped being flippable never flips afterwards.

    Zones are history, not opinion: a level that was traded through stays traded through. If a
    mark could switch back off, a zone broken on Monday would quietly become tradeable again on
    Tuesday — and the flip setup, which exists to trade exactly that history, would lose it.
    """
    tracked = TrackedZone(block=_demand_90_100() if demand else _supply_100_110())
    was_flippable = tracked.flippable
    for index, sample in enumerate(prices):
        before = (tracked.touched, tracked.departed, tracked.flipped, tracked.mitigated)
        OrderBlockDetector._advance(tracked, _grid_candle(index, sample))
        after = (tracked.touched, tracked.departed, tracked.flipped, tracked.mitigated)

        assert all(new or not old for old, new in zip(before, after, strict=True))
        if not was_flippable:
            assert tracked.flipped == before[2]  # no longer flippable: the mark cannot appear
        was_flippable = tracked.flippable

"""Swing detection, checked against series worked out by hand.

The weight here is the *timing*, not just the arithmetic. A pivot is trivial to spot in
hindsight; the whole point is that the engine may only know it `strength` bars late, and the
`Swing` it returns must be stamped with the bar it happened on, not the bar it was found on. The
goldens pin exactly which update confirms which swing, so a change that let a swing surface early
— the anti-lookahead bug — fails loudly.
"""

from datetime import datetime
from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from tradeforge_engine.domain import Candle
from tradeforge_engine.structure import (
    FairValueGap,
    FVGDetector,
    FVGKind,
    Swing,
    SwingDetector,
    SwingKind,
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

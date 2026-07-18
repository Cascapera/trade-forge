"""Market structure: the swing points every Smart Money concept is built on.

A **swing high** is a candle whose high stands above its neighbours by `strength` bars on each
side; a **swing low** is the mirror on the low. They are the pivots that define a trend — higher
highs and higher lows, or the opposite — and they are the levels a break of structure (BOS) or a
change of character (CHoCH) is later measured against. So this is the first brick of the SMC
layer, and everything stacked on it inherits its one hard rule.

**A swing is known only `strength` bars after it happens.** To call bar K a swing high you must
see that the `strength` bars *after* it all stayed below — and those bars have not closed when K
forms. This is not a limitation to work around; it is the anti-lookahead invariant (a decision
at a candle's close acts on the next open) applied to structure. `update` therefore reports a
swing only when it **confirms**, `strength` bars late, and the returned `Swing` carries the time
it actually *occurred*, not the time it was found. A backtest that entered on a swing high the
instant it printed would be trading a level the market had not yet revealed — the exact hazard
`engine-guardian` exists to catch.

**Strict inequality.** A swing high needs its high *strictly* above every neighbour in the
window. Two bars sharing the same high therefore form no swing there — equal highs are not a
pivot, they are **liquidity** (a cluster of stops), a distinct SMC concept handled elsewhere.

**Determinism.** Only `Decimal` highs and lows are compared; there is no arithmetic to round, so
the detector is exact and independent of the engine's decimal context.
"""

from collections import deque
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Final

from tradeforge_engine.domain import Candle, Money


class SwingKind(StrEnum):
    """Which extreme a swing marks."""

    HIGH = "high"
    LOW = "low"


@dataclass(frozen=True, slots=True)
class Swing:
    """One confirmed pivot.

    `time` is when the swing *occurred* — the middle bar of the window — not when it was
    confirmed `strength` bars later. Downstream logic (BOS, CHoCH) needs the level (`price`);
    the time is what keeps a swing honest about the past it belongs to.
    """

    kind: SwingKind
    price: Money
    time: datetime


class SwingDetector:
    """Confirms swing highs and lows incrementally, `strength` bars after they form.

    Feed it one closed candle at a time. It keeps a window of the last `2 * strength + 1` bars;
    when the middle bar is a strict extreme of that window, `update` returns it — a `Swing`
    stamped with the middle bar's time. `last_swing_high` / `last_swing_low` expose the most
    recent confirmed level, which is what a break-of-structure rule will compare price against.
    """

    def __init__(self, *, strength: int = 2) -> None:
        if strength < 1:
            raise ValueError(f"swing strength must be >= 1, got {strength}")
        self._strength = strength
        self._size = 2 * strength + 1
        self._window: deque[Candle] = deque(maxlen=self._size)
        self._last_high: Money | None = None
        self._last_low: Money | None = None

    def update(self, candle: Candle) -> tuple[Swing, ...]:
        """Fold in the newest closed candle and return any swing that *confirms* on this bar.

        A bar can be both — the highest high and the lowest low of a tight window (an outside
        bar) — so the result is a tuple: empty while warming up or on an ordinary bar, one entry
        for a high or a low, two on the rare bar that is both.
        """
        self._window.append(candle)
        if len(self._window) < self._size:
            # Not yet `strength` bars on each side of a candidate — nothing can be confirmed.
            return ()

        middle = self._window[self._strength]
        others = [bar for index, bar in enumerate(self._window) if index != self._strength]

        swings: list[Swing] = []
        if all(middle.high > bar.high for bar in others):
            self._last_high = middle.high
            swings.append(Swing(kind=SwingKind.HIGH, price=middle.high, time=middle.time))
        if all(middle.low < bar.low for bar in others):
            self._last_low = middle.low
            swings.append(Swing(kind=SwingKind.LOW, price=middle.low, time=middle.time))
        return tuple(swings)

    @property
    def last_swing_high(self) -> Money | None:
        """The most recently confirmed swing-high level, or `None` before the first one."""
        return self._last_high

    @property
    def last_swing_low(self) -> Money | None:
        """The most recently confirmed swing-low level, or `None` before the first one."""
        return self._last_low


class FVGKind(StrEnum):
    """Which way a fair value gap points — the direction the impulse that left it was heading."""

    BULLISH = "bullish"
    BEARISH = "bearish"


@dataclass(frozen=True, slots=True)
class FairValueGap:
    """A three-candle imbalance: a band of price the market moved through too fast to trade fairly.

    `top` and `bottom` bound the untraded zone the market tends to return to and "fill". `time` is
    the bar that completed the pattern (the third candle) — the moment the gap becomes known.
    """

    kind: FVGKind
    top: Money
    bottom: Money
    time: datetime


class FVGDetector:
    """Reports fair value gaps as their third candle closes.

    A gap is a strict inefficiency across three consecutive candles: bullish when the first
    candle's high is below the third's low (the middle bar leapt up and left a hole beneath it),
    bearish when the first's low is above the third's high. Unlike a swing, it needs no bars to
    its right — it is defined by the three that end on the current one — so it confirms with no
    lag and no lookahead: a rule acting on it acts on the next open. Only Decimal highs and lows
    are compared, so it is exact and context-independent.
    """

    def __init__(self) -> None:
        self._window: deque[Candle] = deque(maxlen=3)

    def update(self, candle: Candle) -> FairValueGap | None:
        """Fold in the newest candle; return the gap that completes on it, or `None`."""
        self._window.append(candle)
        if len(self._window) < 3:  # noqa: PLR2004 — a gap is a three-candle pattern
            return None

        first, _middle, third = self._window
        if first.high < third.low:
            # Bullish: an untraded band from the first high up to the third low.
            return FairValueGap(
                kind=FVGKind.BULLISH, top=third.low, bottom=first.high, time=third.time
            )
        if first.low > third.high:
            # Bearish: an untraded band from the third high up to the first low.
            return FairValueGap(
                kind=FVGKind.BEARISH, top=first.low, bottom=third.high, time=third.time
            )
        return None


class Trend(StrEnum):
    """The market's structural bias — which way its highs and lows are stepping."""

    BULLISH = "bullish"
    BEARISH = "bearish"


class StructureKind(StrEnum):
    """Whether a break continues the trend (BOS) or turns it (CHoCH)."""

    BOS = "bos"
    CHOCH = "choch"


@dataclass(frozen=True, slots=True)
class StructureBreak:
    """A confirmed break of structure.

    `trend` is the bias the break leaves in force: a BOS keeps it, a CHoCH flips it. `level` is
    the price a candle closed beyond, and `time` is that candle.
    """

    kind: StructureKind
    trend: Trend
    level: Money
    time: datetime


class MarketStructure:
    """Tracks trend through breaks of structure (BOS) and changes of character (CHoCH).

    The method (its author's, pinned by a hand-worked golden):

    * A **BOS** continues the trend. Going up: after the top, two *consecutive* correction bars —
      each a strictly lower high **and** lower low than the bar before, the first measured against
      the top candle — arm it; then a candle **closing** above the top confirms it. Other bars may
      sit between the correction and the close. Every BOS re-anchors the CHoCH level to the lowest
      low of its move (top through break), and lifts the top to the breaking bar.
    * A **CHoCH** turns the trend, and needs no correction: a candle simply **closes** beyond the
      anchor — the lowest low that the last up-move defended (going down), or the highest high the
      last down-move defended (going up). It flips the bias and points the next CHoCH at the high
      or low of the move that just reversed.
    * **Bootstrap.** With no trend yet, the first BOS in either direction — two correction bars and
      a close through — sets the initial bias; from there the sequence runs.

    Only Decimal highs, lows and closes are compared, so it is exact and context-independent, and
    every break is confirmed on a *closed* candle, so a rule acting on one acts on the next open.
    """

    _MIN_CORRECTION: Final = 2

    def __init__(self) -> None:
        self._trend: Trend | None = None
        self._previous: Candle | None = None
        # Up-leg tracking (toward a bullish BOS): top, the lowest low since it, correction count.
        self._up_top: Money | None = None
        self._up_low: Money | None = None
        self._up_corr = 0
        self._up_armed = False
        # Down-leg tracking (toward a bearish BOS).
        self._dn_bottom: Money | None = None
        self._dn_high: Money | None = None
        self._dn_corr = 0
        self._dn_armed = False
        # CHoCH anchors: the level a *closing* candle must cross to turn the trend.
        self._choch_down: Money | None = None  # break below -> bearish CHoCH (while bullish)
        self._choch_up: Money | None = None  # break above -> bullish CHoCH (while bearish)

    def update(self, candle: Candle) -> StructureBreak | None:
        """Fold in one closed candle; return the structure break it confirms, or `None`."""
        previous = self._previous
        self._previous = candle

        # Reversal first: a CHoCH takes precedence over a continuation on the same bar.
        if (
            self._trend is Trend.BULLISH
            and self._choch_down is not None
            and candle.close < self._choch_down
        ):
            break_ = StructureBreak(
                StructureKind.CHOCH, Trend.BEARISH, self._choch_down, candle.time
            )
            self._flip_to_bearish(candle)
            return break_
        if (
            self._trend is Trend.BEARISH
            and self._choch_up is not None
            and candle.close > self._choch_up
        ):
            break_ = StructureBreak(StructureKind.CHOCH, Trend.BULLISH, self._choch_up, candle.time)
            self._flip_to_bullish(candle)
            return break_

        # Continuation / bootstrap: a BOS in whichever direction the trend allows (either, if none).
        if self._trend in (Trend.BULLISH, None):
            broken = self._update_up_leg(candle, previous)
            if broken is not None:
                self._on_bullish_bos(candle)
                return StructureBreak(StructureKind.BOS, Trend.BULLISH, broken, candle.time)
        if self._trend in (Trend.BEARISH, None):
            broken = self._update_down_leg(candle, previous)
            if broken is not None:
                self._on_bearish_bos(candle)
                return StructureBreak(StructureKind.BOS, Trend.BEARISH, broken, candle.time)
        return None

    @property
    def trend(self) -> Trend | None:
        """The current structural bias, or `None` before the first BOS bootstraps it."""
        return self._trend

    # -- up leg (bullish BOS) -------------------------------------------------- #

    def _update_up_leg(self, candle: Candle, previous: Candle | None) -> Money | None:
        """Advance the up-leg; return the broken top level if a bullish BOS confirms, else None."""
        if self._up_top is None or self._up_low is None:
            self._up_top, self._up_low, self._up_corr, self._up_armed = (
                candle.high,
                candle.low,
                0,
                False,
            )
            return None
        if self._up_armed and candle.close > self._up_top:
            self._up_low = min(self._up_low, candle.low)  # the break bar closes the move
            return self._up_top
        if candle.high > self._up_top:  # a new high lifts the top and restarts the correction
            self._up_top, self._up_low, self._up_corr, self._up_armed = (
                candle.high,
                candle.low,
                0,
                False,
            )
            return None
        self._up_low = min(self._up_low, candle.low)
        if previous is not None and candle.high < previous.high and candle.low < previous.low:
            self._up_corr += 1
            if self._up_corr >= self._MIN_CORRECTION:
                self._up_armed = True
        else:
            self._up_corr = 0  # a bar that is not a correction breaks the streak; arming stands
        return None

    def _on_bullish_bos(self, candle: Candle) -> None:
        self._trend = Trend.BULLISH
        self._choch_down = self._up_low  # the low the up-move defended is the next CHoCH anchor
        self._choch_up = None
        self._up_top, self._up_low, self._up_corr, self._up_armed = (
            candle.high,
            candle.low,
            0,
            False,
        )
        self._reset_down_leg()

    def _flip_to_bullish(self, candle: Candle) -> None:
        self._trend = Trend.BULLISH
        self._choch_down = self._dn_bottom  # the low the failed down-move made
        self._choch_up = None
        self._up_top, self._up_low, self._up_corr, self._up_armed = (
            candle.high,
            candle.low,
            0,
            False,
        )
        self._reset_down_leg()

    def _reset_up_leg(self) -> None:
        self._up_top, self._up_low, self._up_corr, self._up_armed = None, None, 0, False

    # -- down leg (bearish BOS) ------------------------------------------------ #

    def _update_down_leg(self, candle: Candle, previous: Candle | None) -> Money | None:
        if self._dn_bottom is None or self._dn_high is None:
            self._dn_bottom, self._dn_high, self._dn_corr, self._dn_armed = (
                candle.low,
                candle.high,
                0,
                False,
            )
            return None
        if self._dn_armed and candle.close < self._dn_bottom:
            self._dn_high = max(self._dn_high, candle.high)
            return self._dn_bottom
        if candle.low < self._dn_bottom:
            self._dn_bottom, self._dn_high, self._dn_corr, self._dn_armed = (
                candle.low,
                candle.high,
                0,
                False,
            )
            return None
        self._dn_high = max(self._dn_high, candle.high)
        if previous is not None and candle.high > previous.high and candle.low > previous.low:
            self._dn_corr += 1
            if self._dn_corr >= self._MIN_CORRECTION:
                self._dn_armed = True
        else:
            self._dn_corr = 0
        return None

    def _on_bearish_bos(self, candle: Candle) -> None:
        self._trend = Trend.BEARISH
        self._choch_up = self._dn_high
        self._choch_down = None
        self._dn_bottom, self._dn_high, self._dn_corr, self._dn_armed = (
            candle.low,
            candle.high,
            0,
            False,
        )
        self._reset_up_leg()

    def _flip_to_bearish(self, candle: Candle) -> None:
        self._trend = Trend.BEARISH
        self._choch_up = self._up_top  # the high the failed up-move made
        self._choch_down = None
        self._dn_bottom, self._dn_high, self._dn_corr, self._dn_armed = (
            candle.low,
            candle.high,
            0,
            False,
        )
        self._reset_up_leg()

    def _reset_down_leg(self) -> None:
        self._dn_bottom, self._dn_high, self._dn_corr, self._dn_armed = None, None, 0, False


class LiquiditySide(StrEnum):
    """Where a run of equal swings stacks the stops a later sweep will hunt."""

    BUY_SIDE = "buy_side"  # equal highs — buy stops rest above
    SELL_SIDE = "sell_side"  # equal lows — sell stops rest below


@dataclass(frozen=True, slots=True)
class LiquidityPool:
    """A cluster of swings resting on one level — a pool of stops the market tends to raid.

    `level` is the cluster's *extreme* (the highest of the equal highs, the lowest of the equal
    lows): the line a sweep must pierce to take every stop behind it. `touches` are the swings that
    built it, oldest first — two make a pool, and each further touch deepens it. `time` is the touch
    that created or last extended the pool, i.e. the moment it became known.
    """

    side: LiquiditySide
    level: Money
    touches: tuple[Swing, ...]
    time: datetime


@dataclass(slots=True)
class _LiquidityCluster:
    """A running cluster of touches on one level.

    `anchor` is the price of the *first* touch and never moves — every later swing is measured
    against it, so the whole pool stays within `tolerance` of one point and a staircase of higher
    highs cannot chain into it. `level` is the running *extreme* (the reported line); `last_bar`
    is the bar of the most recent touch, for staleness.
    """

    anchor: Money
    level: Money
    touches: list[Swing]
    last_bar: int


class LiquidityDetector:
    """Groups equal swing highs (and equal swing lows) into liquidity pools.

    Where the `SwingDetector` *rejects* two highs at the same level — its strict `>` means equal
    highs form no pivot — this is where those equal highs belong: a pool of resting stops. Feed it
    the swings the detector confirms, in order, each with the index of the bar it occurred on. Two
    swings of the same kind whose prices sit within `tolerance` points of the pool's *first* touch
    form a pool; a third or fourth within tolerance deepens it (more touches, more stops — a
    stronger pool). The pool's `level` is the extreme, the line a sweep must clear to sweep every
    stop behind it.

    The tolerance is measured against that first touch (a fixed **anchor**), not the running
    extreme, so the whole pool stays within `tolerance` of one price. That is deliberate: a
    staircase of higher highs (100, 103, 106, … each a step within tolerance of the last) is a
    trend, not equal highs — the old steps have already been swept — so it must *not* collapse into
    one pool. Anchoring to the first touch breaks the staircase into separate levels while a true
    double or triple top still stacks into one.

    `tolerance` is absolute, in the instrument's price points, so the detector stays exact and
    deterministic — only Decimals are compared, nothing is rounded — at the cost of one knob per
    instrument. A pool goes stale after `lookback_bars` with no fresh touch: some setups take a long
    time to arm, so the window is wide by default (200 bars). And because every swing it consumes is
    already confirmed `strength` bars late, the pool inherits the anti-lookahead guarantee for free
    — it can only form on a level the market has already revealed.
    """

    _MIN_TOUCHES_FLOOR: Final = 2

    def __init__(self, *, tolerance: Money, min_touches: int = 2, lookback_bars: int = 200) -> None:
        if tolerance < 0:
            raise ValueError(f"liquidity tolerance must be >= 0, got {tolerance}")
        if min_touches < self._MIN_TOUCHES_FLOOR:
            raise ValueError(f"a pool needs at least 2 touches, got min_touches={min_touches}")
        if lookback_bars < 1:
            raise ValueError(f"lookback_bars must be >= 1, got {lookback_bars}")
        self._tolerance = tolerance
        self._min_touches = min_touches
        self._lookback = lookback_bars
        self._clusters: dict[SwingKind, list[_LiquidityCluster]] = {
            SwingKind.HIGH: [],
            SwingKind.LOW: [],
        }

    def update(self, swing: Swing, bar: int) -> LiquidityPool | None:
        """Fold in one confirmed swing (occurring on `bar`); return the pool it forms or deepens.

        Returns the `LiquidityPool` when this swing brings a cluster to `min_touches` or extends one
        already there, and `None` while a level is still a lone swing. `bar` is the index of the
        candle the swing occurred on — it drives staleness, not the pattern itself.
        """
        # Drop pools whose last touch has aged out of the window — both sides, so a long run of one
        # kind cannot let the other's stale clusters pile up unbounded.
        for kind_clusters in self._clusters.values():
            kind_clusters[:] = [c for c in kind_clusters if bar - c.last_bar <= self._lookback]

        clusters = self._clusters[swing.kind]
        cluster = self._nearest_cluster(clusters, swing.price)
        if cluster is None:
            # No level within tolerance: this swing starts a lone candidate, not yet a pool. Its
            # price is both the anchor (fixed) and the first extreme.
            clusters.append(
                _LiquidityCluster(
                    anchor=swing.price, level=swing.price, touches=[swing], last_bar=bar
                )
            )
            return None

        cluster.touches.append(swing)
        cluster.last_bar = bar
        # The level tracks the extreme, so it stays the line a sweep must clear to take every stop.
        cluster.level = (
            max(cluster.level, swing.price)
            if swing.kind is SwingKind.HIGH
            else min(cluster.level, swing.price)
        )
        if len(cluster.touches) < self._min_touches:
            return None

        side = LiquiditySide.BUY_SIDE if swing.kind is SwingKind.HIGH else LiquiditySide.SELL_SIDE
        return LiquidityPool(
            side=side, level=cluster.level, touches=tuple(cluster.touches), time=swing.time
        )

    def _nearest_cluster(
        self, clusters: list[_LiquidityCluster], price: Money
    ) -> _LiquidityCluster | None:
        """The cluster whose anchor is within tolerance and closest to `price`; ties break to the
        oldest. `None` if none matches. Matching against the fixed anchor (not the drifting extreme)
        keeps a pool inside `tolerance` of one point."""
        best: _LiquidityCluster | None = None
        best_key: tuple[Money, int] | None = None
        for index, cluster in enumerate(clusters):
            distance = abs(price - cluster.anchor)
            if distance <= self._tolerance:
                key = (distance, index)
                if best_key is None or key < best_key:
                    best, best_key = cluster, key
        return best


__all__ = [
    "FVGDetector",
    "FVGKind",
    "FairValueGap",
    "LiquidityDetector",
    "LiquidityPool",
    "LiquiditySide",
    "MarketStructure",
    "StructureBreak",
    "StructureKind",
    "Swing",
    "SwingDetector",
    "SwingKind",
    "Trend",
]

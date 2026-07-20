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

**Determinism.** Comparisons are between `Decimal` prices throughout. The only arithmetic in the
module is a zone's width and the levels derived from it (`top - bottom`, `top + size`), which is
exact for any `Decimal` pair — no rounding, so results do not depend on the engine's decimal
context.
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

    `origin` and `origin_time` mark where the impulse that broke structure *started*: the lowest
    low the up-move came from, or the highest high the down-move came from. Together with `time`
    they bound the impulse leg — the stretch of chart an order block must be found in, and the
    level an entry region is anchored near. It is the same price the next opposite CHoCH is
    anchored to, which is no coincidence: the move begins where the structure it broke was last
    defended.
    """

    kind: StructureKind
    trend: Trend
    level: Money
    time: datetime
    origin: Money
    origin_time: datetime


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
        # Each extreme carries the bar it happened on, so a break can report where its impulse
        # started — the window an order block has to be found in.
        self._up_top: Money | None = None
        self._up_top_time: datetime | None = None
        self._up_low: Money | None = None
        self._up_low_time: datetime | None = None
        self._up_corr = 0
        self._up_armed = False
        # Down-leg tracking (toward a bearish BOS).
        self._dn_bottom: Money | None = None
        self._dn_bottom_time: datetime | None = None
        self._dn_high: Money | None = None
        self._dn_high_time: datetime | None = None
        self._dn_corr = 0
        self._dn_armed = False
        # CHoCH anchors: the level a *closing* candle must cross to turn the trend. After a break
        # the anchor facing back the way it came *is* that break's origin.
        self._choch_down: Money | None = None  # break below -> bearish CHoCH (while bullish)
        self._choch_down_time: datetime | None = None
        self._choch_up: Money | None = None  # break above -> bullish CHoCH (while bearish)
        self._choch_up_time: datetime | None = None

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
            level = self._choch_down
            self._flip_to_bearish(candle)
            return self._break(StructureKind.CHOCH, Trend.BEARISH, level, candle)
        if (
            self._trend is Trend.BEARISH
            and self._choch_up is not None
            and candle.close > self._choch_up
        ):
            level = self._choch_up
            self._flip_to_bullish(candle)
            return self._break(StructureKind.CHOCH, Trend.BULLISH, level, candle)

        # Continuation / bootstrap: a BOS in whichever direction the trend allows (either, if none).
        if self._trend in (Trend.BULLISH, None):
            broken = self._update_up_leg(candle, previous)
            if broken is not None:
                self._on_bullish_bos(candle)
                return self._break(StructureKind.BOS, Trend.BULLISH, broken, candle)
        if self._trend in (Trend.BEARISH, None):
            broken = self._update_down_leg(candle, previous)
            if broken is not None:
                self._on_bearish_bos(candle)
                return self._break(StructureKind.BOS, Trend.BEARISH, broken, candle)
        return None

    def _break(
        self, kind: StructureKind, trend: Trend, level: Money, candle: Candle
    ) -> StructureBreak:
        """Build the break, reading its origin from the anchor the handler just planted.

        Call this *after* the state handler has run: settling the new trend is what records where
        the impulse came from, as the anchor an opposite CHoCH would now have to cross. A bullish
        break leaves `_choch_down` on the low it rose from; a bearish one leaves `_choch_up` on the
        high it fell from.
        """
        if trend is Trend.BULLISH:
            origin, origin_time = self._choch_down, self._choch_down_time
        else:
            origin, origin_time = self._choch_up, self._choch_up_time
        # Both are planted by the handler that just ran; narrowing here is for the type checker.
        assert origin is not None  # noqa: S101
        assert origin_time is not None  # noqa: S101
        return StructureBreak(
            kind=kind,
            trend=trend,
            level=level,
            time=candle.time,
            origin=origin,
            origin_time=origin_time,
        )

    @property
    def trend(self) -> Trend | None:
        """The current structural bias, or `None` before the first BOS bootstraps it."""
        return self._trend

    # -- up leg (bullish BOS) -------------------------------------------------- #

    def _update_up_leg(self, candle: Candle, previous: Candle | None) -> Money | None:
        """Advance the up-leg; return the broken top level if a bullish BOS confirms, else None."""
        if self._up_top is None or self._up_low is None:
            self._restart_up_leg(candle)
            return None
        if self._up_armed and candle.close > self._up_top:
            self._lower_up_low(candle)  # the break bar closes the move
            return self._up_top
        if candle.high > self._up_top:  # a new high lifts the top and restarts the correction
            self._restart_up_leg(candle)
            return None
        self._lower_up_low(candle)
        if previous is not None and candle.high < previous.high and candle.low < previous.low:
            self._up_corr += 1
            if self._up_corr >= self._MIN_CORRECTION:
                self._up_armed = True
        else:
            self._up_corr = 0  # a bar that is not a correction breaks the streak; arming stands
        return None

    def _restart_up_leg(self, candle: Candle) -> None:
        """Start the up-leg over from `candle`: it is both the new top and the new low."""
        self._up_top, self._up_low, self._up_corr, self._up_armed = (
            candle.high,
            candle.low,
            0,
            False,
        )
        self._up_top_time = self._up_low_time = candle.time

    def _lower_up_low(self, candle: Candle) -> None:
        """Drop the up-leg's low to this candle if it went lower. Ties keep the earlier bar — the
        move began the first time price reached that level, not the last."""
        if self._up_low is None or candle.low < self._up_low:
            self._up_low, self._up_low_time = candle.low, candle.time

    def _on_bullish_bos(self, candle: Candle) -> None:
        self._trend = Trend.BULLISH
        # The low the up-move defended is both the next CHoCH anchor and this break's origin.
        self._choch_down, self._choch_down_time = self._up_low, self._up_low_time
        self._choch_up = self._choch_up_time = None
        self._restart_up_leg(candle)
        self._reset_down_leg()

    def _flip_to_bullish(self, candle: Candle) -> None:
        self._trend = Trend.BULLISH
        self._choch_down, self._choch_down_time = self._dn_bottom, self._dn_bottom_time
        self._choch_up = self._choch_up_time = None
        self._restart_up_leg(candle)
        self._reset_down_leg()

    def _reset_up_leg(self) -> None:
        self._up_top, self._up_low, self._up_corr, self._up_armed = None, None, 0, False
        self._up_top_time = self._up_low_time = None

    # -- down leg (bearish BOS) ------------------------------------------------ #

    def _update_down_leg(self, candle: Candle, previous: Candle | None) -> Money | None:
        if self._dn_bottom is None or self._dn_high is None:
            self._restart_down_leg(candle)
            return None
        if self._dn_armed and candle.close < self._dn_bottom:
            self._raise_dn_high(candle)
            return self._dn_bottom
        if candle.low < self._dn_bottom:
            self._restart_down_leg(candle)
            return None
        self._raise_dn_high(candle)
        if previous is not None and candle.high > previous.high and candle.low > previous.low:
            self._dn_corr += 1
            if self._dn_corr >= self._MIN_CORRECTION:
                self._dn_armed = True
        else:
            self._dn_corr = 0
        return None

    def _restart_down_leg(self, candle: Candle) -> None:
        """Start the down-leg over from `candle`: it is both the new bottom and the new high."""
        self._dn_bottom, self._dn_high, self._dn_corr, self._dn_armed = (
            candle.low,
            candle.high,
            0,
            False,
        )
        self._dn_bottom_time = self._dn_high_time = candle.time

    def _raise_dn_high(self, candle: Candle) -> None:
        """Lift the down-leg's high to this candle if it went higher; ties keep the earlier bar."""
        if self._dn_high is None or candle.high > self._dn_high:
            self._dn_high, self._dn_high_time = candle.high, candle.time

    def _on_bearish_bos(self, candle: Candle) -> None:
        self._trend = Trend.BEARISH
        # The high the down-move defended is both the next CHoCH anchor and this break's origin.
        self._choch_up, self._choch_up_time = self._dn_high, self._dn_high_time
        self._choch_down = self._choch_down_time = None
        self._restart_down_leg(candle)
        self._reset_up_leg()

    def _flip_to_bearish(self, candle: Candle) -> None:
        self._trend = Trend.BEARISH
        self._choch_up, self._choch_up_time = self._up_top, self._up_top_time
        self._choch_down = self._choch_down_time = None
        self._restart_down_leg(candle)
        self._reset_up_leg()

    def _reset_down_leg(self) -> None:
        self._dn_bottom, self._dn_high, self._dn_corr, self._dn_armed = None, None, 0, False
        self._dn_bottom_time = self._dn_high_time = None


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


@dataclass(frozen=True, slots=True)
class Sweep:
    """A liquidity pool raided and rejected — the market took the stops and refused the level.

    This is the mirror image of a break of structure. A BOS *closes* beyond a level: the market
    accepted the price and the move continues. A sweep *wicks* beyond it and closes back inside:
    the stops behind the level were filled, nobody defended the new price, and the move was a trap.
    Same pierce, opposite meaning — and the difference is only ever visible at the close.

    `wedge` is the payload the setup actually trades. It holds the rising lows (for a buy-side
    sweep) that carried price into the pool: the trendline of stops belonging to everyone who
    bought the approach. Once the pool is swept and price turns, those are the levels where the
    cascade accelerates. `extreme` is how far the wick reached beyond `level`, and `pierced_at` is
    the bar that reached it — which may be earlier than `time`, the bar that closed back inside and
    made the sweep known.
    """

    side: LiquiditySide
    pool: LiquidityPool
    level: Money
    extreme: Money
    wedge: tuple[Swing, ...]
    pierced_at: datetime
    time: datetime


@dataclass(slots=True)
class _Pierce:
    """A pool whose level has been wicked through, still waiting for a close back inside."""

    extreme: Money
    pierced_at: datetime
    wedge: tuple[Swing, ...]
    deadline: int  # last bar index on which a recovery close still counts


@dataclass(slots=True)
class _Watch:
    """Everything the detector knows about one pool it is watching.

    `inside` is the state that makes a sweep a *sweep*: price must be on the protected side of the
    level (at or below a buy-side pool) before going through it can mean anything. Without it a
    pool the market broke long ago — one it has been trading above for a hundred bars — would
    report a sweep on the first pullback that closed under it.

    Note the comparison is *not* strict, unlike the pierce and the recovery. Those ask "did price
    reject the level?"; this asks "is price on the protected side?", and a close exactly at the
    level is not acceptance. Reusing the strict test here would let a single doji closing on the
    level disarm a pool — most likely on a round number, which is exactly where stops pile up.
    """

    pool: LiquidityPool
    tracked_at: int  # bar index of the most recent `track`, for staleness
    inside: bool
    pierce: _Pierce | None = None


class _WedgeTracker:
    """The zig-zag of minor pivots, and whether its tail forms a wedge losing volatility.

    A wedge here is the author's definition: at least `min_pivots` **ascending lows** (a rising
    trendline) whose **corrections shrink monotonically** — 2.0, then 1.5, then 1.0 — i.e. price
    grinding higher while giving back less and less. That decay is the tell: buyers are being
    squeezed into a smaller and smaller range right under a shelf of stops. The bearish mirror is
    descending highs with shrinking rallies.

    The pivots are `SwingDetector(strength=1)` swings, not the layer's usual strength-2 ones. A
    wedge is made of *minor* pivots — one bar on each side — so it is recognised a single bar after
    its last leg instead of two or three, which matters when the sweep follows immediately. Reusing
    the swing detector rather than writing a second pivot rule keeps one definition of "a low" in
    the codebase and inherits its anti-lookahead confirmation for free.

    Pivots are normalised into a strict zig-zag: two lows in a row collapse to the lower, two highs
    to the higher. Without that a wedge could be measured against a "correction" that never had a
    high between its two lows.
    """

    # The wedge only ever reads a tail of the sequence, so the history stays bounded.
    _MAX_PIVOTS: Final = 64
    # An outside bar is the only candle that confirms two pivots — a high and a low — at once.
    _OUTSIDE_BAR_PIVOTS: Final = 2

    def __init__(self, *, min_pivots: int) -> None:
        self._detector = SwingDetector(strength=1)
        self._pivots: list[Swing] = []
        self._min_pivots = min_pivots

    def update(self, candle: Candle) -> None:
        """Fold in one closed candle, confirming any minor pivot it completes."""
        pivots = self._detector.update(candle)
        if len(pivots) == self._OUTSIDE_BAR_PIVOTS:
            if not self._pivots:
                # No tail to order the pair against, and taking both would seed the sequence with a
                # high and a low from the same bar — the degenerate shape the ordering below
                # exists to avoid. An outside bar cannot open a zig-zag; wait for a clean pivot.
                return
            # An outside bar prints both extremes at once. Feed the one matching the current tail
            # first, so it collapses into it under the "keep the extreme" rule and the other lands
            # on top: the sequence stays alternating *and* keeps the real high or low.
            #
            # This is the author's rule (an outside bar's range is real price movement), not a
            # safe default — it is not one. Keeping the extreme can *raise* the turning point of an
            # older counter-move, and because the backward scan requires each earlier counter-move
            # to be larger, inflating an old one can turn a growing sequence into a shrinking one
            # and admit a wedge that is not there. Dropping the pivot instead understates the next
            # counter-move, which fabricates a shrink just as easily. Both directions can flip the
            # verdict either way; there is no conservative choice here, only a stated one.
            last_kind = self._pivots[-1].kind
            pivots = tuple(sorted(pivots, key=lambda pivot: pivot.kind is not last_kind))
        for pivot in pivots:
            self._append(pivot)

    def _append(self, pivot: Swing) -> None:
        if self._pivots and self._pivots[-1].kind is pivot.kind:
            # Same kind twice: keep the more extreme one so the sequence stays a strict zig-zag.
            last = self._pivots[-1]
            more_extreme = (
                pivot.price > last.price
                if pivot.kind is SwingKind.HIGH
                else pivot.price < last.price
            )
            if more_extreme:
                self._pivots[-1] = pivot
            return
        self._pivots.append(pivot)
        if len(self._pivots) > self._MAX_PIVOTS:
            del self._pivots[: -self._MAX_PIVOTS]

    def bullish_wedge(self) -> tuple[Swing, ...] | None:
        """The rising lows of the current bullish wedge, oldest first — `None` if there is none."""
        return self._wedge(SwingKind.LOW)

    def bearish_wedge(self) -> tuple[Swing, ...] | None:
        """The falling highs of the current bearish wedge, oldest first — `None` if none."""
        return self._wedge(SwingKind.HIGH)

    def _wedge(self, kind: SwingKind) -> tuple[Swing, ...] | None:
        """Longest tail of same-kind pivots advancing in `kind`'s direction with shrinking
        counter-moves. Walks backwards from the newest pivot: read forwards the counter-moves must
        shrink, so read backwards each one must be strictly larger than the one after it."""
        anchors = [index for index, pivot in enumerate(self._pivots) if pivot.kind is kind]
        if len(anchors) < self._min_pivots:
            return None

        start = len(anchors) - 1
        counter_moves: list[Money] = []
        while start > 0:
            # `update` keeps the sequence strictly alternating, so consecutive same-kind anchors are
            # always two apart and the pivot between them is the counter-move's turning point.
            earlier, later = anchors[start - 1], anchors[start]
            first, second = self._pivots[earlier], self._pivots[later]
            advancing = (
                second.price > first.price if kind is SwingKind.LOW else second.price < first.price
            )
            if not advancing:
                break
            turn = self._pivots[earlier + 1]
            move = turn.price - second.price if kind is SwingKind.LOW else second.price - turn.price
            if counter_moves and move <= counter_moves[-1]:
                break
            counter_moves.append(move)
            start -= 1

        if len(anchors) - start < self._min_pivots:
            return None
        return tuple(self._pivots[index] for index in anchors[start:])


class SweepDetector:
    """Detects liquidity sweeps: a wedge into a pool, a wick through it, a close back inside.

    Feed it every closed candle via `update`, and every pool the `LiquidityDetector` reports via
    `track`. It reports a `Sweep` on the bar that completes the pattern:

    1. **Price inside the level.** A bar must first close on the protected side — at or below a
       buy-side pool, at or above a sell-side one. Stops only rest behind a level the market has
       not yet taken, so a pool price is already trading beyond was broken long ago and can no
       longer be swept.
    2. **A wedge into the pool.** Ascending lows with shrinking corrections for a buy-side pool
       (the mirror below). This is a *precondition*, by the author's rule: a wick through a level
       out of nowhere is noise, while a wick through a level that a squeezed, low-volatility grind
       walked into is a trap with a trendline of stops beneath it.
    3. **A pierce.** A bar's high goes strictly above the pool's `level` (low below, for sell-side),
       coming from inside. The wedge is checked at this bar — the moment the trap is sprung.
    4. **A close back inside**, on the piercing bar or within `recovery_bars - 1` bars after it.
       Rejection can take more than one candle: one bar overshoots, the next drags price back. If
       the window expires with no close back inside, the market *accepted* the level — that is a
       break, not a sweep, and the pool is dropped rather than reported.

    The pierce and the recovery are strict, matching the rest of this module: a close exactly at
    the level has neither pierced nor recovered. Step 1 is the deliberate exception — being *on*
    the level is not being beyond it, so it leaves a pool armed; see `_Watch`. Pools are keyed by
    their first touch, so a pool that deepens (a third or fourth equal high) updates the tracked
    level in place instead of stacking a duplicate, and any pool not re-tracked for
    `lookback_bars` is discarded.

    **Caller contract.** Feed a bar to `update`, then `track` whatever pools that bar produced —
    a pool is not known until the bar confirming its last touch has closed. Following that order
    is what keeps the anti-lookahead invariant intact, and a raid on the very next bar is then
    detected normally.

    As a backstop the detector also refuses to sweep a pool with a bar at or before the pool's
    last *touch*, which catches the grossest violation. It is only a backstop: a touch is
    confirmed `strength` bars after it occurs, so `pool.time` sits in the past by construction and
    this check cannot police the confirmation lag. The call order above is the real guarantee.
    """

    _MIN_WEDGE_FLOOR: Final = 3

    def __init__(
        self, *, recovery_bars: int = 3, min_wedge_pivots: int = 3, lookback_bars: int = 200
    ) -> None:
        if recovery_bars < 1:
            raise ValueError(f"recovery_bars must be >= 1, got {recovery_bars}")
        if min_wedge_pivots < self._MIN_WEDGE_FLOOR:
            raise ValueError(
                f"a wedge needs at least 3 pivots to show two shrinking corrections, "
                f"got min_wedge_pivots={min_wedge_pivots}"
            )
        if lookback_bars < 1:
            raise ValueError(f"lookback_bars must be >= 1, got {lookback_bars}")
        self._recovery = recovery_bars
        self._lookback = lookback_bars
        self._wedges = _WedgeTracker(min_pivots=min_wedge_pivots)
        self._watches: dict[tuple[LiquiditySide, datetime], _Watch] = {}
        self._last_close: Money | None = None
        self._bar = -1

    def track(self, pool: LiquidityPool) -> None:
        """Watch `pool` for a sweep, replacing any earlier state for the same pool.

        Pools are identified by their first touch, so re-reporting a deepened pool refreshes its
        level rather than tracking the same stops twice. A pool whose level moved is a different
        line to defend, so any pierce in flight against the old level is discarded.

        A newly watched pool is armed from the last close already seen, not from scratch. Waiting
        for one more bar would blind the detector for exactly one candle — and the raid on the bar
        right after a pool confirms is the cleanest instance of the pattern, not an edge case.
        """
        key = (pool.side, pool.touches[0].time)
        known = self._watches.get(key)
        if known is not None and known.pool.level == pool.level:
            known.pool = pool
            known.tracked_at = self._bar
            return
        self._watches[key] = _Watch(
            pool=pool, tracked_at=self._bar, inside=self._is_inside(pool, self._last_close)
        )

    @staticmethod
    def _is_inside(pool: LiquidityPool, close: Money | None) -> bool:
        """Whether `close` sits on the pool's protected side — at the level counts as inside."""
        if close is None:
            return False
        return close <= pool.level if pool.side is LiquiditySide.BUY_SIDE else close >= pool.level

    def update(self, candle: Candle) -> tuple[Sweep, ...]:
        """Fold in one closed candle; return every sweep it completes, oldest level first.

        One bar can raid more than one pool — a single push can clear stops at 101 and at 103 — and
        each is its own event with its own stops and its own extreme. Reporting only one would
        silently drop the other, so the result is a tuple, like `SwingDetector.update`. It is
        ordered by level so the output does not depend on the order pools were tracked in.
        """
        self._bar += 1
        self._wedges.update(candle)
        self._expire()

        completed: list[Sweep] = []
        for key, watch in list(self._watches.items()):
            sweep = self._advance(watch, candle)
            if sweep is not None:
                completed.append(sweep)
                del self._watches[key]

        self._last_close = candle.close
        # Level and side alone can tie — an aged-out pool and a fresh one can share a price — so
        # the first touch breaks it, keeping the order independent of how pools were tracked.
        completed.sort(key=lambda sweep: (sweep.level, sweep.side, sweep.pool.touches[0].time))
        return tuple(completed)

    def _advance(self, watch: _Watch, candle: Candle) -> Sweep | None:
        """Move one pool through the state machine on this candle."""
        pool = watch.pool
        # Backstop only: a bar cannot raid a pool built on a touch it has not yet reached. The
        # real anti-lookahead guarantee is the caller contract in the class docstring.
        if candle.time <= pool.time:
            return None

        buy_side = pool.side is LiquiditySide.BUY_SIDE
        recovered = candle.close < pool.level if buy_side else candle.close > pool.level

        if watch.pierce is None:
            # Read `inside` as it stood *before* this bar, then let this bar's close set it: a
            # pierce has to come from the protected side, not merely end up there.
            was_inside = watch.inside
            watch.inside = self._is_inside(pool, candle.close)
            if not was_inside:
                return None
            pierced = candle.high > pool.level if buy_side else candle.low < pool.level
            if not pierced:
                return None
            # The wedge is a precondition, checked exactly here: at the bar that springs the trap.
            wedge = self._wedges.bullish_wedge() if buy_side else self._wedges.bearish_wedge()
            if wedge is None:
                return None
            watch.pierce = _Pierce(
                extreme=candle.high if buy_side else candle.low,
                pierced_at=candle.time,
                wedge=wedge,
                deadline=self._bar + self._recovery - 1,
            )
        else:
            # Still in the window: the wick can run further before price is dragged back.
            watch.pierce.extreme = (
                max(watch.pierce.extreme, candle.high)
                if buy_side
                else min(watch.pierce.extreme, candle.low)
            )

        if not recovered:
            return None

        return Sweep(
            side=pool.side,
            pool=pool,
            level=pool.level,
            extreme=watch.pierce.extreme,
            wedge=watch.pierce.wedge,
            pierced_at=watch.pierce.pierced_at,
            time=candle.time,
        )

    def _expire(self) -> None:
        """Drop pools whose recovery window has run out — the market accepted the level, so it was
        a break, not a sweep — and pools not re-tracked for `lookback_bars`."""
        for key, watch in list(self._watches.items()):
            timed_out = watch.pierce is not None and self._bar > watch.pierce.deadline
            if timed_out or self._bar - watch.tracked_at > self._lookback:
                del self._watches[key]


class ZoneKind(StrEnum):
    """Which side of the book an order block marks."""

    DEMAND = "demand"  # left by a buy impulse — price broke structure upward
    SUPPLY = "supply"  # left by a sell impulse


@dataclass(frozen=True, slots=True)
class OrderBlock:
    """A supply or demand zone: the last candle before the institutions moved price away.

    Marked the author's way, **by price inefficiency**. Where an impulse breaks structure *and*
    leaves a gap, the candle immediately before that gap is where the size was worked — the
    footprint of the position that caused the move. (The book notes a second, popular convention,
    the last opposite candle before the break, and says to pick one and stay with it. This is the
    one.)

    `top` and `bottom` bound the zone. For demand the base is the marking candle's own range,
    extended *down* to the gap candle's low whenever that low ran deeper — the wick is part of
    where they worked, and dropping it would put the zone above where price actually turned.
    Supply mirrors it upward.

    `time` is the bar the zone is drawn on; `confirmed_at` is the bar whose close broke structure
    and revealed it. Both matter and they differ: the zone belongs to the past, but nothing could
    know it was a zone until the break confirmed, so a strategy may only act from `confirmed_at`.

    `primary` marks the first gap event of the impulse. One impulse can leave several zones, but
    only when the gapping *pauses* — a bar that opens no new gap — and then resumes; an unbroken
    run of gaps is one event, not one zone per bar. The first is the primary, the rest secondary,
    and whether secondaries may be traded is a decision for the strategy, not for this detector.
    """

    kind: ZoneKind
    top: Money
    bottom: Money
    time: datetime
    confirmed_at: datetime
    break_kind: StructureKind
    primary: bool


@dataclass(slots=True)
class TrackedZone:
    """An order block and what price has since done to it.

    Two marks, both permanent once set, and they answer different questions.

    `mitigated` says the zone is spent **for its own side**. It happens either the healthy way —
    price came back, touched it, and then *closed* a full zone-width clear of it, so the orders
    resting there have been filled and the move they fund is already underway — or the failed way:
    price closed straight through, so whatever was defending the level is gone.

    A touch is a wick; everything that *decides* is a close. That split is what lets a zone survive
    a news spike: a bar can trade far outside it and still end up inside, and the market has not
    left. It also removes the intrabar guess — a single bar whose wick both reaches the zone and
    runs a width past it says nothing about which came first, so nothing is inferred from it.

    `flipped` says price went *through* the zone, by wick or by close. A demand zone that price
    traded down through has trapped every buyer who took it, and on the way back those buyers sell
    to get out flat. That is the raw material of the flip setup: a zone marked flipped is where a
    trade *against* the prevailing trend becomes reasonable. A wick through is enough to mark it
    and leaves the zone still usable; a close through marks it **and** mitigates it.
    """

    block: OrderBlock
    touched: bool = False
    departed: bool = False
    flipped: bool = False
    mitigated: bool = False

    @property
    def usable(self) -> bool:
        """Whether the zone still stands for the side it was marked on."""
        return not self.mitigated

    @property
    def flippable(self) -> bool:
        """Whether breaking this zone would still count as a flip.

        A flip has to be *abrupt* — one move that arrives and takes the zone out. Price that came
        in, backed away, and only later returned to break it is a different story: the zone was
        already being negotiated, so breaking it traps nobody the way a single decisive drive
        does. A zone price has left after touching, or that is already mitigated, is past that.
        """
        return not self.departed and not self.mitigated


@dataclass(frozen=True, slots=True)
class _GapEvent:
    """One confirmed gap, kept with the two candles a zone would be marked from."""

    index: int  # bar index of the gap's third candle — adjacency is what groups a run
    kind: FVGKind
    time: datetime
    first: Candle  # c1 — the candle the zone is drawn on
    second: Candle  # c2 — the impulse candle whose wick may extend the zone


class OrderBlockDetector:
    """Marks supply and demand zones from the impulse legs that break structure.

    Feed it every closed candle together with whatever `MarketStructure.update` returned for that
    same candle. On a break it looks back over the impulse leg — from `origin_time` to the breaking
    bar, the stretch the break itself reports — for gaps pointing the same way as the break, and
    returns the zones they mark, primary first.

    Three rules, all the author's:

    * **The gap must be in the impulse leg.** A gap left somewhere else is not the footprint of the
      move that broke structure, so it marks nothing here.
    * **The zone is the candle before the gap**, widened by the gap candle's wick when that wick
      ran past it.
    * **Consecutive gaps are one event.** Price gapping on bar after bar is one continuous push;
      it takes a pause — a bar that completes no gap — before a fresh gap starts a second zone.
      Without this an impulsive leg would mark a zone on nearly every bar of itself.

    Nothing is reported until a break confirms on a closed candle, so a zone is only ever known
    from `confirmed_at` onward, and a strategy acting on it acts at the next open.

    Every zone it marks is then kept and followed — see `zones` and `TrackedZone` — because a zone
    that has been used up, or traded through, is not thereby uninteresting: the flip setup trades
    exactly those.

    **A zone is born clean.** The impulse leg that revealed it is not replayed against it, and the
    breaking bar does not touch it either. That is deliberate twice over. Nobody could know the
    zone existed until the break confirmed, so counting the leg against it would be acting on
    hindsight; and the leg *is* price leaving the zone — by the gap's own construction the impulse
    candle overlaps the marking candle, so replaying it would mark almost every zone touched, and
    often mitigated, at birth.
    """

    # A leg longer than this is not an impulse any more; the cap also bounds memory when a long
    # stretch of chart passes with no break at all.
    _MAX_LOOKBACK: Final = 500
    # Zones outlive their usefulness but stay readable for the flip setup; keep the recent ones.
    _MAX_ZONES: Final = 200

    def __init__(self) -> None:
        self._fvgs = FVGDetector()
        self._window: deque[Candle] = deque(maxlen=3)
        self._gaps: list[_GapEvent] = []
        self._zones: list[TrackedZone] = []
        self._index = -1

    @property
    def zones(self) -> tuple[TrackedZone, ...]:
        """Every zone marked so far, oldest first, each with what price has done to it since."""
        return tuple(self._zones)

    def update(self, candle: Candle, break_: StructureBreak | None) -> tuple[OrderBlock, ...]:
        """Fold in one closed candle and the break it confirmed (if any); return the zones marked.

        Pass the `StructureBreak` that `MarketStructure.update` returned for *this* candle, or
        `None`. Returns the zones the break reveals, primary first, and `()` on every other bar.
        """
        self._index += 1
        self._window.append(candle)
        # Advance the zones already on the book *before* marking new ones: a zone cannot be
        # touched or broken by the very bar whose close first revealed it.
        for tracked in self._zones:
            self._advance(tracked, candle)

        gap = self._fvgs.update(candle)
        if gap is not None and len(self._window) == self._window.maxlen:
            first, second, _third = self._window
            self._gaps.append(
                _GapEvent(
                    index=self._index, kind=gap.kind, time=gap.time, first=first, second=second
                )
            )
        self._gaps = [g for g in self._gaps if self._index - g.index <= self._MAX_LOOKBACK]

        if break_ is None:
            return ()

        wanted = FVGKind.BULLISH if break_.trend is Trend.BULLISH else FVGKind.BEARISH
        in_leg = [
            gap_event
            for gap_event in self._gaps
            if gap_event.kind is wanted
            and gap_event.first.time >= break_.origin_time
            and gap_event.time <= break_.time
        ]
        # Every gap up to this break belongs to the leg that just ended: a new leg starts at the
        # breaking bar, so none of these can qualify again.
        self._gaps = [g for g in self._gaps if g.time > break_.time]

        marked = tuple(
            self._zone(run[0], break_, primary=position == 0)
            for position, run in enumerate(self._runs(in_leg))
        )
        self._zones.extend(TrackedZone(block=block) for block in marked)
        if len(self._zones) > self._MAX_ZONES:
            del self._zones[: -self._MAX_ZONES]
        return marked

    @staticmethod
    def _advance(tracked: TrackedZone, candle: Candle) -> None:
        """Fold one candle into a zone's state. Every mark is permanent once set."""
        block = tracked.block
        size = block.top - block.bottom
        demand = block.kind is ZoneKind.DEMAND

        # Read before this bar changes anything. A bar that closes clean through both flips the
        # zone and spends it, so the flip has to be judged on the history *behind* the bar — if
        # this bar's own mitigation counted first, it would veto its own flip.
        was_flippable = tracked.flippable

        # Closing beyond the far side always spends the zone, whatever else is true of it. A level
        # the market has closed through is gone: whether it was flippable, already departed, or
        # untouched makes no difference to that.
        through = candle.close < block.bottom if demand else candle.close > block.top
        tracked.mitigated = tracked.mitigated or through

        # The flip mark is the narrow one: only a still-flippable zone can be flipped, and the
        # pierce is by wick — the drive has to arrive and take the zone out in one go.
        pierced = candle.low < block.bottom if demand else candle.high > block.top
        if was_flippable and pierced:
            tracked.flipped = True

        # Reaching the zone at all counts as a touch — by wick, like everything else here.
        reached = candle.low <= block.top if demand else candle.high >= block.bottom
        tracked.touched = tracked.touched or reached
        # Leaving it is judged on the *close*, not the wick — the same line this module draws
        # everywhere else. News can spike price far out of a zone and drag it straight back inside
        # within one bar; that spike is not the market backing away from the level. Only a bar
        # that ends up beyond the near edge has actually left. So a bar closing lower inside the
        # zone, then one that pokes up but closes down and carries on through, is still one drive.
        #
        # `touched` is updated first on purpose: a single bar that dips into the zone and closes
        # clear of it has touched and left, exactly as two bars doing the same would. Reading the
        # old value here would make the flip mark depend on whether the move happened to land in
        # one candle or two — the same strategy would flip on M5 and not on M15.
        # Set *after* the flip mark above, and that ordering is semantic, not cosmetic: a bar that
        # arrives, takes the zone out and closes clear must not use its own departure to veto its
        # own flip. Same principle as reading `was_flippable` at the top. Moving this block up
        # would silently break it.
        cleared = candle.close > block.top if demand else candle.close < block.bottom
        if tracked.touched and cleared:
            tracked.departed = True

        # Spent the healthy way: touched, then driven off by a full zone-width. The zone did its
        # job, and the orders that were resting in it are now in the market.
        #
        # Measured on the close, like every other decision in this module. A news wick can throw
        # price a long way past a zone and bring it back inside the same bar; treating that spike
        # as the zone having worked would kill a level the market never actually left — and with
        # it any flip that level still had in it, since a mitigated zone can no longer flip.
        if not tracked.touched:
            return
        driven_off = (
            candle.close > block.top + size if demand else candle.close < block.bottom - size
        )
        tracked.mitigated = tracked.mitigated or driven_off

    @staticmethod
    def _runs(gaps: list[_GapEvent]) -> list[list[_GapEvent]]:
        """Split gaps into runs of consecutive bars. A break in the indices is the pause that
        separates one gap event from the next."""
        runs: list[list[_GapEvent]] = []
        for gap in gaps:
            if runs and gap.index == runs[-1][-1].index + 1:
                runs[-1].append(gap)
            else:
                runs.append([gap])
        return runs

    @staticmethod
    def _zone(gap: _GapEvent, break_: StructureBreak, *, primary: bool) -> OrderBlock:
        """Mark the zone on the candle before `gap`, extended by the gap candle's wick."""
        marking, impulse = gap.first, gap.second
        if gap.kind is FVGKind.BULLISH:
            kind = ZoneKind.DEMAND
            top, bottom = marking.high, min(marking.low, impulse.low)
        else:
            kind = ZoneKind.SUPPLY
            top, bottom = max(marking.high, impulse.high), marking.low
        return OrderBlock(
            kind=kind,
            top=top,
            bottom=bottom,
            time=marking.time,
            confirmed_at=break_.time,
            break_kind=break_.kind,
            primary=primary,
        )


__all__ = [
    "FVGDetector",
    "FVGKind",
    "FairValueGap",
    "LiquidityDetector",
    "LiquidityPool",
    "LiquiditySide",
    "MarketStructure",
    "OrderBlock",
    "OrderBlockDetector",
    "StructureBreak",
    "StructureKind",
    "Sweep",
    "SweepDetector",
    "Swing",
    "SwingDetector",
    "SwingKind",
    "TrackedZone",
    "Trend",
    "ZoneKind",
]

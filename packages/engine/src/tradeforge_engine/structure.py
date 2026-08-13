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

    **The middle candle must also close beyond the gap's origin**, and that is the author's rule
    rather than the textbook one — his indicator is in `docs/referencia/indicador-regioes-order-
    block.md`, and where the two disagree it wins. The geometric test alone admits a shape he does
    not trade: three bars that leave an untraded band while the middle one closes back *inside*
    it, which is a hole price is still arguing over rather than one it left behind. Requiring the
    close makes the middle bar commit to the direction that made the hole.

    Measured over 3480 real AAPL H1 candles: the geometric test alone reports 482 bullish and 388
    bearish gaps, his rule 428 and 347 — so 95 of them, better than one in six, were shapes the
    engine marked and he does not.
    """

    def __init__(self) -> None:
        self._window: deque[Candle] = deque(maxlen=3)

    def update(self, candle: Candle) -> FairValueGap | None:
        """Fold in the newest candle; return the gap that completes on it, or `None`."""
        self._window.append(candle)
        if len(self._window) < 3:  # noqa: PLR2004 — a gap is a three-candle pattern
            return None

        first, middle, third = self._window
        if first.high < third.low and middle.close > first.high:
            # Bullish: an untraded band from the first high up to the third low, left by a middle
            # bar that closed clear of it.
            return FairValueGap(
                kind=FVGKind.BULLISH, top=third.low, bottom=first.high, time=third.time
            )
        if first.low > third.high and middle.close < first.low:
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
    level_time: datetime
    """The bar that *set* the level this break crossed — where the structure was last defended.

    Distinct from `time`, which is the bar that crossed it, and the two together are what make a
    break legible on a chart: a horizontal line from `level_time` to `time` is the structure that
    held, drawn for exactly as long as it held. Without this, the level can only be drawn as a
    line of arbitrary length, and how long the structure stood — the thing that says whether a
    break means anything — is not recoverable from the record.
    """

    origin: Money
    origin_time: datetime


class MarketStructure:
    """Tracks trend through breaks of structure (BOS) and changes of character (CHoCH).

    **A transcription of the author's own Profit indicator**, which he has traded from for
    years. That provenance is the design: this is not a reading of what SMC "should" mean, it
    is the machine whose marks he recognises, ported so that a backtest disagrees with his
    screen about nothing. Where the two could differ, the indicator wins.

    Its state is four running extremes and two armed levels, and the whole method falls out of
    when each is reset:

    * **`_low_down` / `_high_down`** — the extremes tracked while the bias is down;
      **`_low_up` / `_high_up`** — the same going up. All four move on every bar, and only a
      break resets them.
    * **A BOS is armed by a two-bar counter-move and confirmed by a close.** Going up: two
      consecutive bars each making a lower high **and** a lower low arm it at `_high_up`, the
      highest high so far; a candle **closing above** that level confirms. Going down mirrors
      it. Only the first counter-move arms — a second one while a level is already armed is
      ignored, so the level waits for its close rather than being dragged along by the
      pullback.
    * **Confirming a BOS plants the anchor the opposite CHoCH will need**, and *that* is the
      rule this class exists to get right: a bullish BOS sets the bearish CHoCH anchor to
      `_lowest_since_armed` — **the lowest low between the bar that armed the BOS and the bar
      that confirmed it**. Not the lowest low of the whole leg, and not the lowest low since
      the last high: only the stretch the pullback and its resolution occupy.
    * **A CHoCH needs no counter-move.** A close through the anchor turns the bias, and plants
      the opposite anchor from whichever level is standing — the armed BOS if there is one, the
      running extreme if there is not.

    **One deliberate departure from the Pascal.** There, an unset level is the number zero, so
    the first close of a series is "above" the bullish anchor and fires a CHoCH at 0.00, and
    the next bar fires its mirror. On a chart those two marks are harmless paint. In an engine
    they are two orders on a level that is not a price, so an anchor that has never been planted
    is `None` here and breaks nothing until it exists. Everything else is literal.
    """

    def __init__(self) -> None:
        self._trend: Trend | None = None
        self._previous: Candle | None = None
        self._before_previous: Candle | None = None

        # The four running extremes. `_down` are read while the bias is bearish, `_up` while it
        # is bullish; both pairs advance on every bar regardless, exactly as the indicator does.
        self._low_down: Money | None = None
        self._low_down_time: datetime | None = None
        self._high_down: Money | None = None
        self._low_up: Money | None = None
        self._low_up_time: datetime | None = None
        self._high_up: Money | None = None
        self._high_up_time: datetime | None = None

        # The armed levels: the price a close has to pass for a BOS to confirm. `None` means
        # nothing is armed — the Pascal writes 0 and 999999 for these, which are sentinels and
        # not prices.
        self._armed_low: Money | None = None
        self._armed_low_time: datetime | None = None
        self._armed_high: Money | None = None
        self._armed_high_time: datetime | None = None

        # The CHoCH anchors, each planted by the BOS that confirmed in the opposite direction.
        self._choch_down: Money | None = None
        self._choch_down_time: datetime | None = None
        self._choch_up: Money | None = None
        self._choch_up_time: datetime | None = None

        # The extremes accumulated **only while a BOS is armed** — the heart of the method.
        # While nothing is armed they simply follow the current bar, so that the moment one
        # arms they start from the bar beside it; once armed they only extend.
        self._lowest_since_armed: Money | None = None
        self._lowest_since_armed_time: datetime | None = None
        self._highest_since_armed: Money | None = None
        self._highest_since_armed_time: datetime | None = None

    def update(self, candle: Candle) -> StructureBreak | None:
        """Fold in one closed candle; return the break it confirmed, or `None`.

        At most one break per bar. The Pascal can emit a BOS and a CHoCH on the same candle —
        the BOS plants the opposite anchor, and the very same close can already be through it —
        but a bar that both continues and reverses the trend is not something a strategy can
        act on twice, and the second mark is the one that decides the bias. So the reversal is
        reported and the continuation is folded into the state, which is what the next bar sees.
        """
        previous, before = self._previous, self._before_previous
        self._before_previous, self._previous = previous, candle

        self._advance_extremes(candle)
        if previous is None or before is None:
            # The counter-move rule reads three bars. Nothing can be armed before there are.
            return None

        rising = (
            candle.high > previous.high
            and candle.low > previous.low
            and previous.high > before.high
            and previous.low > before.low
        )
        falling = (
            candle.high < previous.high
            and candle.low < previous.low
            and previous.high < before.high
            and previous.low < before.low
        )

        if self._trend is Trend.BULLISH:
            return self._on_bullish_bar(candle, rising=rising, falling=falling)
        return self._on_bearish_bar(candle, rising=rising, falling=falling)

    # ----------------------------------------------------------------------- #

    def _advance_extremes(self, candle: Candle) -> None:
        """The four running extremes, moved on every bar before anything is decided."""
        if self._low_down is None or candle.low < self._low_down:
            self._low_down, self._low_down_time = candle.low, candle.time
        if self._high_down is None or candle.high > self._high_down:
            self._high_down = candle.high
        if self._low_up is None or candle.low < self._low_up:
            self._low_up, self._low_up_time = candle.low, candle.time
        if self._high_up is None or candle.high > self._high_up:
            self._high_up, self._high_up_time = candle.high, candle.time

    def _on_bullish_bar(
        self, candle: Candle, *, rising: bool, falling: bool
    ) -> StructureBreak | None:
        """The `DIR = 1` branch, in its original order."""
        if rising:
            # Housekeeping for the other direction: the pullback that would arm a bearish BOS.
            self._armed_low, self._armed_low_time = self._low_down, self._low_down_time
            self._high_down = candle.high

        if falling and self._armed_high is None:
            # The counter-move arms the BOS at the highest high reached so far. Only the first
            # one: a second pullback while a level waits does not move it.
            self._armed_high, self._armed_high_time = self._high_up, self._high_up_time
            self._low_up, self._low_up_time = candle.low, candle.time

        confirmed: StructureBreak | None = None
        if self._armed_high is not None and candle.close > self._armed_high:
            confirmed = self._emit(
                StructureKind.BOS,
                Trend.BULLISH,
                self._armed_high,
                self._armed_high_time,
                candle,
                anchor=(self._lowest_since_armed, self._lowest_since_armed_time),
            )
            self._armed_high = self._armed_high_time = None
            # The rule this class exists for: the bearish CHoCH now sits on the lowest low
            # between the arming and this close.
            self._choch_down = self._lowest_since_armed
            self._choch_down_time = self._lowest_since_armed_time

        self._track_lowest(candle, armed=self._armed_high is not None)

        if self._choch_down is not None and candle.close < self._choch_down:
            reversal = self._emit(
                StructureKind.CHOCH,
                Trend.BEARISH,
                self._choch_down,
                self._choch_down_time,
                candle,
                anchor=(
                    (self._high_up, self._high_up_time)
                    if self._armed_high is None
                    else (self._armed_high, self._armed_high_time)
                ),
            )
            self._choch_up = reversal.origin
            self._choch_up_time = reversal.origin_time
            self._low_down, self._low_down_time = candle.low, candle.time
            self._armed_low = self._armed_low_time = None
            self._trend = Trend.BEARISH
            return reversal
        return confirmed

    def _on_bearish_bar(
        self, candle: Candle, *, rising: bool, falling: bool
    ) -> StructureBreak | None:
        """The `DIR = -1` branch — the mirror, and also where a fresh series starts."""
        if rising and self._armed_low is None:
            self._armed_low, self._armed_low_time = self._low_down, self._low_down_time
            self._high_down = candle.high

        if falling:
            self._armed_high, self._armed_high_time = self._high_up, self._high_up_time
            self._low_up, self._low_up_time = candle.low, candle.time

        confirmed: StructureBreak | None = None
        if self._armed_low is not None and candle.close < self._armed_low:
            confirmed = self._emit(
                StructureKind.BOS,
                Trend.BEARISH,
                self._armed_low,
                self._armed_low_time,
                candle,
                anchor=(self._highest_since_armed, self._highest_since_armed_time),
            )
            self._armed_low = self._armed_low_time = None
            self._choch_up = self._highest_since_armed
            self._choch_up_time = self._highest_since_armed_time

        self._track_highest(candle, armed=self._armed_low is not None)

        if self._choch_up is not None and candle.close > self._choch_up:
            reversal = self._emit(
                StructureKind.CHOCH,
                Trend.BULLISH,
                self._choch_up,
                self._choch_up_time,
                candle,
                anchor=(
                    (self._low_down, self._low_down_time)
                    if self._armed_low is None
                    else (self._armed_low, self._armed_low_time)
                ),
            )
            self._choch_down = reversal.origin
            self._choch_down_time = reversal.origin_time
            self._high_up, self._high_up_time = candle.high, candle.time
            self._armed_high = self._armed_high_time = None
            self._trend = Trend.BULLISH
            return reversal
        return confirmed

    def _track_lowest(self, candle: Candle, *, armed: bool) -> None:
        """Follow the current low while nothing is armed; extend it while something is.

        Called **after** the arm and confirm checks, exactly where the Pascal calls it. That
        ordering is the whole reason the anchor covers the pullback: on the bar that arms, this
        has already been following the bar beside it, so the window opens there rather than at
        the arming bar itself.
        """
        if not armed or self._lowest_since_armed is None or candle.low < self._lowest_since_armed:
            self._lowest_since_armed, self._lowest_since_armed_time = candle.low, candle.time

    def _track_highest(self, candle: Candle, *, armed: bool) -> None:
        """The mirror of `_track_lowest`."""
        if (
            not armed
            or self._highest_since_armed is None
            or candle.high > self._highest_since_armed
        ):
            self._highest_since_armed, self._highest_since_armed_time = candle.high, candle.time

    def _emit(  # noqa: PLR0913 — one axis each; a break is this many facts
        self,
        kind: StructureKind,
        trend: Trend,
        level: Money | None,
        level_time: datetime | None,
        candle: Candle,
        *,
        anchor: tuple[Money | None, datetime | None],
    ) -> StructureBreak:
        """Build the break. `anchor` is the extreme the move came from — its `origin`.

        The origin is where the impulse started, which is both the stretch an order block must
        be found in and the level the opposite CHoCH will be anchored to. Those are the same
        price for the same reason: the move begins where the structure it broke was last
        defended.
        """
        origin, origin_time = anchor
        # Every one of these is planted in the same statement as its price; the narrowing is
        # for the type checker, not for a case that can happen.
        assert level is not None  # noqa: S101
        assert level_time is not None  # noqa: S101
        assert origin is not None  # noqa: S101
        assert origin_time is not None  # noqa: S101
        return StructureBreak(
            kind=kind,
            trend=trend,
            level=level,
            time=candle.time,
            level_time=level_time,
            origin=origin,
            origin_time=origin_time,
        )

    @property
    def trend(self) -> Trend | None:
        """The bias in force, or `None` before the first break settles one."""
        return self._trend


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

    `top` and `bottom` bound the zone, and they are the marking candle's **own high and low** —
    nothing added, nothing extended. His indicator draws exactly `[low[2], high[2]]`.

    An earlier version of this widened the zone to swallow the gap candle's wick wherever that ran
    past the marking candle, on the reasoning that the wick was part of where the size was worked.
    That was not his rule, and it was not harmless: the edge is where the order rests, so a
    widened zone is an order at a price he would not have used. It moved 22% of zones.

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
    """An order block and the one thing that can happen to it.

    `mitigated` says the region is spent: **price came back and touched its entry edge**, by wick,
    once. The near edge is the top of a demand zone and the bottom of a supply zone — the side
    price has to return to in order to trade there — so the first touch is the moment the orders
    resting in the region are taken. There is nothing to add after that; a region is offered once.

    That is his indicator's rule verbatim (`docs/referencia/indicador-regioes-order-block.md`):

        ob.bull ? low <= ob.topo : high >= ob.fundo

    An earlier version of this carried three more marks — `touched`, `departed`, `flipped` — and a
    second way to die, `driven_off`, where a close a full zone-width clear counted as the region
    having done its job. None of that is his method. The extra marks existed to feed a *flip*
    setup, trading the region price had traded through against whoever was trapped in it; asked
    directly, he said a dead region does not serve a flip, and the setup had never been built.
    """

    block: OrderBlock
    mitigated: bool = False

    mitigated_at: datetime | None = None
    """The bar that took it — the **first** touch, never a later one.

    Carried for the chart, which draws a region from the bar that marked it to the bar that
    took it, so the rectangle's length is how long it stood. Nothing in the engine's decisions
    reads this: `usable` is the whole rule, and it only ever asks the boolean.

    ⚠️ First, and that is the entire subtlety of stamping it. `mitigated` is folded forward with
    `or`, so it stays true through every later bar that also reaches the edge — and a stamp
    written on the same terms would keep moving to the most recent one. The region would then be
    drawn as having survived until the last time price happened to be there, which in a range is
    hundreds of bars past the touch that actually killed it.
    """

    @property
    def usable(self) -> bool:
        """Whether the region still stands — nothing has come back to take it."""
        return not self.mitigated


@dataclass(slots=True)
class _Region:
    """A gap's region, followed from the bar the gap completed.

    This is the piece that has to exist *before* a break reveals anything. A region is born the
    moment its gap completes, and price starts working it from that bar — so by the time a break
    makes it interesting, it may already be gone. Waiting for the break to start watching is what
    made the engine offer regions the market had spent days earlier.
    """

    index: int  # bar index of the gap's third candle
    kind: FVGKind
    marking: Candle  # c1 — the candle the region is drawn on; its range *is* the region
    mitigated: bool = False

    @property
    def top(self) -> Money:
        return self.marking.high

    @property
    def bottom(self) -> Money:
        return self.marking.low

    @property
    def near_edge(self) -> Money:
        """The side price must come back to: a demand region's top, a supply region's bottom."""
        return self.top if self.kind is FVGKind.BULLISH else self.bottom

    def touched_by(self, candle: Candle) -> bool:
        """His rule, on one candle: the first wick to reach the entry edge takes the region."""
        if self.kind is FVGKind.BULLISH:
            return candle.low <= self.near_edge
        return candle.high >= self.near_edge


class OrderBlockDetector:
    """Marks supply and demand zones from the impulse legs that break structure.

    Feed it every closed candle together with whatever `MarketStructure.update` returned for that
    same candle. On a break it looks back over the impulse leg — from `origin_time` to the breaking
    bar, the stretch the break itself reports — for gaps pointing the same way as the break, and
    returns the zones they mark, primary first.

    Four rules, all the author's, and his indicator is the reference for the first three:
    `docs/referencia/indicador-regioes-order-block.md`.

    * **The region is born at the gap, not at the break.** It is the marking candle's own range,
      and it starts being worked the moment the gap completes. This is the rule the detector used
      to get wrong, and it was not a detail: a break confirms a median of 16 bars after the gap
      (282 at the worst), so a region could be traded away for a week before anything asked about
      it. Offered fresh at the break, it was an order at a level the market had already taken.
    * **A region dies at the first touch of its entry edge**, by wick — see `TrackedZone`.
    * **Consecutive gaps make one region.** While the gap condition holds bar after bar it is one
      continuous push, and only the first bar of the run marks anything. His indicator latches on
      exactly this, per direction, and so does this.
    * **Only regions inside the impulse leg are offered.** A gap left elsewhere is not the
      footprint of the move that broke structure. The leg is `origin_time` to the breaking bar,
      which the break reports itself.

    So a region has two lives. It is *followed* from its gap, by anyone or no one; it is *offered*
    when a break reveals it, and only if it is still standing then. Nothing is reported before the
    break, so a strategy still only ever acts from `confirmed_at` onward and acts at the next open.
    """

    # A leg longer than this is not an impulse any more; the cap also bounds memory when a long
    # stretch of chart passes with no break at all.
    _MAX_LOOKBACK: Final = 500
    # Offered regions stay readable to strategies for a while after they die; keep the recent ones.
    _MAX_ZONES: Final = 200

    def __init__(self) -> None:
        self._fvgs = FVGDetector()
        self._window: deque[Candle] = deque(maxlen=3)
        self._regions: list[_Region] = []
        self._zones: list[TrackedZone] = []
        self._index = -1
        # The author's latch, one per direction: a run of gapping bars marks one region, not one
        # per bar. Cleared by any bar that completes no gap that way.
        self._gapping: dict[FVGKind, bool] = {FVGKind.BULLISH: False, FVGKind.BEARISH: False}

    @property
    def zones(self) -> tuple[TrackedZone, ...]:
        """Every region offered so far, oldest first, each with whether price has taken it."""
        return tuple(self._zones)

    def update(self, candle: Candle, break_: StructureBreak | None) -> tuple[OrderBlock, ...]:
        """Fold in one closed candle and the break it confirmed (if any); return the zones offered.

        Pass the `StructureBreak` that `MarketStructure.update` returned for *this* candle, or
        `None`. Returns the regions the break reveals, primary first, and `()` on every other bar.
        """
        self._index += 1
        self._window.append(candle)
        # Everything already being followed advances *before* this bar can mark anything new. A
        # region cannot be taken by the bar that created it — his gap condition puts price clear
        # of the region on that bar, so the first touch can only come later.
        for tracked in self._zones:
            self._advance(tracked, candle)
        for region in self._regions:
            region.mitigated = region.mitigated or region.touched_by(candle)

        gap = self._fvgs.update(candle)
        for kind in self._gapping:
            self._gapping[kind] = self._gapping[kind] and gap is not None and gap.kind is kind
        if gap is not None and not self._gapping[gap.kind]:
            if len(self._window) == self._window.maxlen:
                marking, _impulse, _third = self._window
                self._regions.append(_Region(index=self._index, kind=gap.kind, marking=marking))
            self._gapping[gap.kind] = True
        self._regions = [r for r in self._regions if self._index - r.index <= self._MAX_LOOKBACK]

        if break_ is None:
            return ()

        wanted = FVGKind.BULLISH if break_.trend is Trend.BULLISH else FVGKind.BEARISH
        in_leg = [
            region
            for region in self._regions
            if region.kind is wanted
            and not region.mitigated
            and region.marking.time >= break_.origin_time
        ]
        # Every region up to this break belonged to the leg that just ended, taken or not: a new
        # leg starts at the breaking bar, so none of them can be offered again.
        self._regions = [r for r in self._regions if r.index > self._index]

        marked = tuple(
            self._zone(region, break_, primary=position == 0)
            for position, region in enumerate(in_leg)
        )
        self._zones.extend(TrackedZone(block=block) for block in marked)
        if len(self._zones) > self._MAX_ZONES:
            del self._zones[: -self._MAX_ZONES]
        return marked

    @staticmethod
    def _advance(tracked: TrackedZone, candle: Candle) -> None:
        """Fold one candle into an offered region: the first touch of its entry edge takes it.

        The whole rule, and permanent once set. What was here before — a flip mark, a departure
        mark, and a second death by being driven a full width clear — was the engine's invention
        and is documented as such on `TrackedZone`.
        """
        block = tracked.block
        if block.kind is ZoneKind.DEMAND:
            reached = candle.low <= block.top
        else:
            reached = candle.high >= block.bottom
        # The stamp goes on the transition, not on the condition. Writing it whenever `reached`
        # is true would keep overwriting it with every later visit — see `TrackedZone`.
        if reached and not tracked.mitigated:
            tracked.mitigated_at = candle.time
        tracked.mitigated = tracked.mitigated or reached

    @staticmethod
    def _zone(region: _Region, break_: StructureBreak, *, primary: bool) -> OrderBlock:
        """Offer a region that is still standing — its marking candle's range, edge to edge."""
        kind = ZoneKind.DEMAND if region.kind is FVGKind.BULLISH else ZoneKind.SUPPLY
        return OrderBlock(
            kind=kind,
            top=region.top,
            bottom=region.bottom,
            time=region.marking.time,
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

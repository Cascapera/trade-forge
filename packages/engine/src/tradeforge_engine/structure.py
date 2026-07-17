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


__all__ = [
    "FVGDetector",
    "FVGKind",
    "FairValueGap",
    "Swing",
    "SwingDetector",
    "SwingKind",
]

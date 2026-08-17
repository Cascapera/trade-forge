"""Indicators: incremental state, O(1) per closed candle.

An indicator is a tiny state machine. You fold one closed candle into it at a time, and it
tells you its current value — or `None`, while it is still warming up. It never sees the
series, only the next bar, which is the same discipline the whole engine runs on and the
only shape that can also run live.

**Why incremental and not "recompute a window".** A 200-period average could be "sum the
last 200 closes and divide" on every bar. That is O(period) per bar and O(period·N) over a
run; a decade of M1 is five million bars, and the O(N²) version simply does not finish.
The incremental form keeps a running state and pays O(1) per bar. It is not an
optimisation added later — it is the only form that works, and the only form that has a
live counterpart, where there is no window to recompute.

**Why `value()` returns `None` during warm-up.** A 20-period average has no meaning on bar
3. Returning a half-formed number there is how a strategy ends up trading on a value that
does not exist yet; returning `None` makes the warm-up a fact the caller has to handle, not
a silent zero. A condition that reads a warming-up indicator is simply false (see
`expressions`), so no trade fires until every indicator it names has a value.

**Why `Decimal`.** Same reason as the rest of the engine: an average feeds a comparison
that decides a trade, and binary floating point drifts. The division in a mean and the
smoothing in an EMA both run in `Decimal`, deterministically, under the engine's pinned
context when driven by `run()`.

Adding a new indicator (RSI, ATR, ADX in phase 2) is a new class plus one line in
`INDICATOR_BUILDERS` — never an edit to the loop or the compiler. That is ADR-03 in
practice: new blocks without touching the core.
"""

import collections
import datetime as dt
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Final

from tradeforge_engine.domain import ZERO, Candle, Money
from tradeforge_engine.errors import EngineError
from tradeforge_engine.protocols import CompositeIndicator, Indicator

_PRICE_SOURCES: Final = frozenset({"open", "high", "low", "close"})


def _price(candle: Candle, source: str) -> Money:
    """Read one field of the candle. `source` is validated upstream by the schema."""
    if source not in _PRICE_SOURCES:
        raise EngineError(
            f"unknown price source {source!r}; expected one of {sorted(_PRICE_SOURCES)}"
        )
    value: Money = getattr(candle, source)
    return value


class SMA:
    """Simple moving average — the mean of the last `period` values of one price source.

    State is a ring buffer of the window and a running sum. `update` adds the new value and
    subtracts the one that just fell out of the window — O(1), no re-summation. `value()` is
    `None` until the window is full, then `sum / period`.
    """

    def __init__(self, *, period: int, source: str = "close") -> None:
        if period < 1:
            raise ValueError(f"SMA period must be >= 1, got {period}")
        self._period = period
        self._source = source
        self._window: collections.deque[Money] = collections.deque(maxlen=period)
        self._sum: Money = ZERO

    def update(self, candle: Candle) -> None:
        price = _price(candle, self._source)
        # The deque evicts its oldest element on append once it is full. Subtract that
        # element from the running sum *before* it disappears, or the sum drifts upward
        # forever and the average silently climbs with it.
        if len(self._window) == self._period:
            self._sum -= self._window[0]
        self._window.append(price)
        self._sum += price

    def value(self) -> Money | None:
        if len(self._window) < self._period:
            return None
        return self._sum / self._period


class EMA:
    """Exponential moving average — a weighted mean that never forgets, but forgets fast.

    `ema = price·alpha + ema_prev·(1 - alpha)`, with `alpha = 2 / (period + 1)`. Each new
    price gets weight `alpha`; everything before it decays geometrically. Unlike the SMA it
    has no hard window — the whole history is in the current value — which is exactly why it
    is O(1) with a single number of state.

    **Seeding.** The recurrence needs a previous value to start from, and starting it at the
    first price alone makes the early output lurch. The standard, and the one used here: warm
    up by accumulating the first `period` prices as a simple average, and seed the EMA with
    that mean on the `period`-th bar. `value()` is `None` until then. Which bar you seed on
    and what you seed with are the two classic places an EMA implementation disagrees with a
    spreadsheet — pinned here, and checked by a golden test.
    """

    def __init__(self, *, period: int, source: str = "close") -> None:
        if period < 1:
            raise ValueError(f"EMA period must be >= 1, got {period}")
        self._period = period
        self._source = source
        # `alpha` is NOT computed here. `__init__` runs inside `compile_strategy`, which is
        # *outside* the pinned decimal context that only `run()` installs — and `2/(period+1)`
        # is inexact for almost every period, so computing it here would bake in whatever
        # precision the ambient process happens to carry. Two workers compiling the same
        # strategy under different global contexts would then produce EMAs that differ in the
        # last place, and a crossover that flips one bar early takes the whole equity curve
        # with it. So it is computed lazily in `update()`, which only ever runs under `run()`'s
        # `ENGINE_CONTEXT` — the same context every other number in the EMA is rounded in.
        self._alpha: Money | None = None
        self._seed_count = 0
        self._seed_sum: Money = ZERO
        self._value: Money | None = None

    def update(self, candle: Candle) -> None:
        price = _price(candle, self._source)
        if self._value is None:
            # Still seeding: the EMA is the simple mean of the first `period` prices, and
            # only becomes live on the bar that completes them.
            self._seed_count += 1
            self._seed_sum += price
            if self._seed_count == self._period:
                self._value = self._seed_sum / self._period
            return
        if self._alpha is None:
            self._alpha = Decimal(2) / (self._period + 1)
        # Written as `prev + alpha·(price - prev)`, not `price·alpha + prev·(1 - alpha)`.
        # The two are algebraically identical, but the latter rounds `price·alpha` and
        # `prev·(1 - alpha)` separately, and on a flat series (price == prev) the two ULPs
        # do not cancel — the EMA of a constant drifts a last place off that constant. This
        # form collapses to `prev + alpha·0 = prev` exactly, so a flat input stays flat.
        self._value = self._value + self._alpha * (price - self._value)

    def value(self) -> Money | None:
        return self._value


class RSI:
    """Relative Strength Index (Wilder, 1978) — momentum as a bounded 0-100 oscillator.

    Each bar's close-to-close change is a gain (an up move) or a loss (a down move, taken as a
    positive magnitude) — never both. RSI compares their smoothed averages:

        RS  = avg_gain / avg_loss
        RSI = 100 - 100 / (1 + RS)

    so a run of only gains drives RSI toward 100 and only losses toward 0.

    **Wilder smoothing — not the `EMA` in this file.** The averages use Wilder's method, an EMA
    whose weight is `1 / period`, not the `2 / (period + 1)` of `EMA`. For period 14 that is
    ~0.071 against ~0.133 — nearly double — and swapping one for the other is the most common
    reason an RSI disagrees with every charting tool. It is written in the same stable
    `prev + (x - prev) / period` form `EMA` uses, so a flat stretch neither drifts nor surprises.

    **Seeding** mirrors the EMA: the first average is the simple mean of the first `period` gains
    (and of the first `period` losses), which needs `period` changes — `period + 1` closes — so
    `value()` is `None` until then.

    **avg_loss == 0.** No down move in the window makes RS a division by zero; the limit is
    RSI = 100 (RS → ∞), which is also what an only-rising or flat series should read. Pinned here
    and checked by a golden.
    """

    def __init__(self, *, period: int, source: str = "close") -> None:
        if period < 1:
            raise ValueError(f"RSI period must be >= 1, got {period}")
        self._period = period
        self._source = source
        self._previous: Money | None = None
        self._seed_count = 0
        self._seed_gain: Money = ZERO
        self._seed_loss: Money = ZERO
        self._avg_gain: Money | None = None
        self._avg_loss: Money | None = None

    def update(self, candle: Candle) -> None:
        price = _price(candle, self._source)
        if self._previous is None:
            # The first bar is a level with no prior close to change from — no gain, no loss.
            self._previous = price
            return
        change = price - self._previous
        self._previous = price
        gain = change if change > ZERO else ZERO
        loss = -change if change < ZERO else ZERO
        if self._avg_gain is None or self._avg_loss is None:
            # Seeding: the first average is the simple mean of the first `period` moves.
            self._seed_count += 1
            self._seed_gain += gain
            self._seed_loss += loss
            if self._seed_count == self._period:
                self._avg_gain = self._seed_gain / self._period
                self._avg_loss = self._seed_loss / self._period
            return
        # Wilder smoothing: an EMA with alpha = 1/period, in the stable increment form. The
        # division runs under `run()`'s pinned context, like every other number in the engine.
        self._avg_gain = self._avg_gain + (gain - self._avg_gain) / self._period
        self._avg_loss = self._avg_loss + (loss - self._avg_loss) / self._period

    def value(self) -> Money | None:
        if self._avg_gain is None or self._avg_loss is None:
            return None
        if self._avg_loss == ZERO:
            # No losses in the window: RS → ∞, RSI → 100. Also the only-rising / flat case.
            return Decimal(100)
        rs = self._avg_gain / self._avg_loss
        return Decimal(100) - Decimal(100) / (Decimal(1) + rs)


class ATR:
    """Average True Range (Wilder, 1978) — how far this market moves in a bar, in price.

    **True Range first, and it is why this indicator reads a candle rather than a price.** A
    bar's range is not `high - low`: a market that gapped opens away from yesterday's close, and
    the distance it travelled includes the gap. So TR is the widest of three spans:

        TR = max(high - low, |high - previous close|, |low - previous close|)

    On a bar that gapped up, the second term wins and the first understates the move by the size
    of the gap. The first bar of a run has no previous close, so its TR is `high - low` — the
    only reading available, and it is seeded rather than skipped so a short run still warms up.

    **Wilder smoothing — not the `EMA` in this file.** The average uses weight `1 / period`, not
    `2 / (period + 1)`. For period 14 that is ~0.071 against ~0.133, nearly double, and swapping
    them is the most common reason an ATR disagrees with every charting tool. The golden test
    pins a series where the two answers differ (5 against 6) precisely so the confusion cannot
    survive here.

    **Seeding** is the simple mean of the first `period` true ranges, which needs `period` bars.

    ⚠️ **No `source` parameter, unlike SMA and EMA.** ATR reads high, low *and* the previous
    close by definition; an "ATR of the close" is not a smaller version of this, it is a
    different measurement. Offering the parameter would let a document ask for something this
    class cannot mean.
    """

    def __init__(self, *, period: int) -> None:
        if period < 1:
            raise ValueError(f"ATR period must be >= 1, got {period}")
        self._period = period
        self._previous_close: Money | None = None
        self._seed_count = 0
        self._seed_total: Money = ZERO
        self._average: Money | None = None

    def _true_range(self, candle: Candle) -> Money:
        span = candle.high - candle.low
        if self._previous_close is None:
            return span
        return max(
            span,
            abs(candle.high - self._previous_close),
            abs(candle.low - self._previous_close),
        )

    def update(self, candle: Candle) -> None:
        true_range = self._true_range(candle)
        self._previous_close = candle.close
        if self._average is None:
            self._seed_count += 1
            self._seed_total += true_range
            if self._seed_count == self._period:
                self._average = self._seed_total / self._period
            return
        # Wilder smoothing, in the same stable increment form `EMA` and `RSI` use.
        self._average = self._average + (true_range - self._average) / self._period

    def value(self) -> Money | None:
        return self._average


class _Extreme:
    """The highest high or the lowest low of the last `period` bars, in O(1) amortised.

    **A monotonic deque, not a scan of the window.** Taking `max()` over the window on every bar
    is O(period) per bar, which this module's whole discipline exists to avoid — a 200-period
    channel over a decade of M1 is a billion comparisons. The deque holds only the candidates
    that could still become the extreme: a new value pops every value it beats, because those
    can never be the answer again while the newcomer is in the window. Each value is pushed once
    and popped once, so the amortised cost is constant however long the window is.

    ⚠️ **The window ends at the previous bar: the current one is not in it.** This is the whole
    difference between a channel that can be broken and one that cannot, and the inclusive
    version was measured failing before this line was written. Fold the current bar in first and
    `HIGHEST(20) >= high` holds by construction on every bar of every market — so
    `breaks_above(price.high, channel)` is constantly false, and `between(close, low, high)` is
    constantly true, because `lower <= low <= close <= high <= upper`. Both rails of the feature
    degenerate at once, and neither says a word: the backtest runs and reports no trades.

    The escape a charting platform offers is a shifted read (`ta.highest(high, 20)[1]`), and the
    DSL v1 has no shift for an indicator ref — `shift` exists only inside `Trend`. So the level
    is defined where it can be used. The cost is a documented deviation from Pine, whose
    `ta.highest` does include the current bar; a general `{"ref": ..., "shift": N}` would let
    both readings coexist and is in the backlog.

    Consequence for warm-up: the first answer lands on bar `period` (zero-based), one bar later
    than an inclusive window, because `period` bars must have *closed before* this one.
    """

    def __init__(self, *, period: int, field: str, keep_higher: bool) -> None:
        if period < 1:
            raise ValueError(f"period must be >= 1, got {period}")
        self._period = period
        self._field = field
        self._keep_higher = keep_higher
        self._seen = 0
        # (index, value) pairs, ordered from the extreme outwards. The index is what lets a
        # value leave when the window slides past it.
        self._candidates: collections.deque[tuple[int, Money]] = collections.deque()
        # The answer for the bar being processed, captured before that bar joins the window.
        self._current: Money | None = None

    def _beats(self, incoming: Money, held: Money) -> bool:
        # ⚠️ `>=`, not `>`: a newer bar holding the *same* extreme outlives the older one, so the
        # older can never be the answer again and is dropped. Strict comparison is not wrong —
        # the front of the deque would still be a correct value — it just parks one candidate per
        # tie, which on a flat market is one per bar of the window. Memory, not correctness.
        return incoming >= held if self._keep_higher else incoming <= held

    def update(self, candle: Candle) -> None:
        # ⚠️ Read **before** the current bar joins: the level this bar is measured against is the
        # extreme of the bars that closed before it. `None` until `period` of them have, like
        # every other indicator here — a channel answering on bar 3 of a 20-bar lookback would
        # report the extreme of three bars as if it were the extreme of twenty.
        self._current = self._candidates[0][1] if self._seen >= self._period else None

        value = _price(candle, self._field)
        while self._candidates and self._beats(value, self._candidates[-1][1]):
            self._candidates.pop()
        self._candidates.append((self._seen, value))
        # Drop the front once it is older than the window. `_seen` is the index of the bar just
        # added, so the oldest bar still inside is `_seen - period + 1`.
        if self._candidates[0][0] <= self._seen - self._period:
            self._candidates.popleft()
        self._seen += 1

    def value(self) -> Money | None:
        return self._current


class Highest(_Extreme):
    """The highest **high** of the `period` bars that closed *before* this one.

    The upper rail of a breakout channel, and the exclusion is what makes it one — see
    `_Extreme`. Highs rather than closes, so the level is where price actually traded and a
    break of it fires on the wick; requiring a close beyond it is the same channel compared
    against `price.close` instead.
    """

    def __init__(self, *, period: int) -> None:
        super().__init__(period=period, field="high", keep_higher=True)


class Lowest(_Extreme):
    """The lowest **low** of the `period` bars that closed *before* this one — the lower rail."""

    def __init__(self, *, period: int) -> None:
        super().__init__(period=period, field="low", keep_higher=False)


class _FoldsOnce:
    """The repeat guard every composite needs — see `protocols.CompositeIndicator`.

    A single-valued indicator has one caller and never meets this. A composite is reached
    through one channel per component, and the overlay reader drives every channel it was
    handed, so the same closed bar arrives once per component. Counting it three times would
    build an ADX from three times the bars anyone asked for — a number that looks like an ADX.

    ⚠️ **A bar strictly older than the last one raises rather than being ignored.** Nothing in
    this engine can currently produce that: the loop feeds a run in order, and the overlay
    reader replays a sorted window. Ignoring it silently would be the same shape of failure as
    the one this class exists to stop, only harder to find — the state would simply be built
    from a different set of bars than the caller believes. Failing loudly on a case that cannot
    happen costs nothing until the day it can.
    """

    def __init__(self) -> None:
        self._folded_at: dt.datetime | None = None

    def _is_repeat(self, candle: Candle) -> bool:
        if self._folded_at is not None:
            if candle.time == self._folded_at:
                return True
            if candle.time < self._folded_at:
                raise EngineError(
                    f"candle at {candle.time.isoformat()} is older than the last one folded "
                    f"({self._folded_at.isoformat()}); indicators read a series forwards"
                )
        self._folded_at = candle.time
        return False


class Bollinger(_FoldsOnce):
    """Bollinger Bands — a moving average with a volatility envelope around it.

        middle = SMA(period)
        upper  = middle + deviations * sd
        lower  = middle - deviations * sd

    where `sd` is the standard deviation of the same `period` prices the average was taken over.
    The width is the reading: the bands squeeze when the market goes quiet and flare when it
    moves, so "price touched the upper band" means something different in each regime, which is
    the whole point of the indicator over a fixed-width channel like `HIGHEST`/`LOWEST`.

    **Population sd (divide by n), not sample sd (n-1).** The window *is* the population being
    described — these `period` bars, not a sample drawn from some larger set of bars we are
    inferring about. It is also what every charting platform computes, and the difference is
    real: at period 20 the sample form is 2.6% wider, which moves the band by a fifth of a
    standard deviation and turns a touch into a non-touch on the exact bars the strategy cares
    about.

    **`sd` from `sum` and `sum_of_squares`, not a pass over the window.** `variance = E[x2] - E[x]2`
    keeps the O(1) discipline of this module. The classic objection is catastrophic
    cancellation: the two terms are nearly equal when prices are large and variance is small, and
    subtracting them throws away the leading digits they share.

    That objection was **measured** here rather than argued, against a two-pass sum of squared
    deviations over the same windows, at 28 digits. The two agree to the last digit — zero
    relative error — for every price scale up to 1e9, at period 20 and at period 200. The first
    divergence is at 1e10 (0.039% on the standard deviation), it reaches 4% at 1e11, and from
    1e12 the one-pass form returns exactly zero for a window that genuinely moves. Real
    instruments live at 1e0 (GBPUSD) to 1e5 (BTCUSD), so this leaves four orders of magnitude of
    margin — and the boundary is written down so the day something reads a price near 1e10, the
    form has to change rather than be trusted. Under binary floating point this would already be
    the wrong implementation at forex prices, which is worth knowing rather than inheriting.

    ⚠️ **The variance is clamped at zero before the square root**, because `Decimal.sqrt()`
    raises `InvalidOperation` on a negative and the subtraction above can produce one. A *flat*
    window cannot: `n·x / n` is exact, so the two terms cancel to exactly zero. What does reach
    it is a **nearly** flat window at a high price scale — measured from 1e6 upward, when the
    window's spread is orders of magnitude below one tick — where cancellation eats the whole
    value and tips it a last place below zero. Removing the clamp turns that into a crash inside
    an indicator, and the true variance there is zero to every digit that survived, so zero is
    also the right answer rather than a papered-over one.
    """

    # ⚠️ Ordered **primary first**, and that is a contract rather than a style choice: a reader
    # with no knowledge of what a band is uses this order to decide which line is the subject and
    # which are its envelope (the chart draws the first solid and the rest dashed). Alphabetical
    # or "upper, middle, lower" order would make the upper band the subject of its own average.
    COMPONENTS: Final = ("middle", "upper", "lower")

    def __init__(self, *, period: int, source: str = "close", deviations: Money) -> None:
        if period < 1:
            raise ValueError(f"Bollinger period must be >= 1, got {period}")
        if deviations <= ZERO:
            raise ValueError(f"Bollinger deviations must be > 0, got {deviations}")
        super().__init__()
        self._period = period
        self._source = source
        # Unlike `EMA`'s alpha, this one is safe to hold from construction: it arrives as an
        # exact decimal (the builder parses it through `str`) and is never *computed* here, so
        # there is no inexact division to bake the ambient context's precision into.
        self._deviations = deviations
        self._window: collections.deque[Money] = collections.deque(maxlen=period)
        self._sum: Money = ZERO
        self._sum_of_squares: Money = ZERO

    def update(self, candle: Candle) -> None:
        if self._is_repeat(candle):
            return
        price = _price(candle, self._source)
        # Same eviction discipline as `SMA`: subtract what is about to fall out of the window
        # *before* the deque drops it, or both running totals drift upward for ever.
        if len(self._window) == self._period:
            oldest = self._window[0]
            self._sum -= oldest
            self._sum_of_squares -= oldest * oldest
        self._window.append(price)
        self._sum += price
        self._sum_of_squares += price * price

    def components(self) -> Mapping[str, Money | None]:
        if len(self._window) < self._period:
            return {"middle": None, "upper": None, "lower": None}
        middle = self._sum / self._period
        # Clamped: a negative here is only ever a rounding artefact of a flat window, and
        # `Decimal.sqrt()` raises on it — see the class docstring.
        variance = max(self._sum_of_squares / self._period - middle * middle, ZERO)
        spread = self._deviations * variance.sqrt()
        return {"middle": middle, "upper": middle + spread, "lower": middle - spread}


class ADX(_FoldsOnce):
    """Average Directional Index (Wilder, 1978) — how *strongly* a market is trending, not which
    way.

    Three readings, and the pair is what the headline is built from:

        +DM = up move,   if this bar's high rose more than its low fell, else 0
        -DM = down move, if this bar's low fell more than its high rose, else 0
        +DI = 100 · smoothed(+DM) / smoothed(TR)      (and -DI likewise)
        DX  = 100 · |+DI - -DI| / (+DI + -DI)
        ADX = smoothed(DX)

    `+DI` and `-DI` say which side is winning; `DX` says by how much, as a share of all the
    movement there was; `ADX` smooths `DX` so a single decisive bar cannot read as a trend.
    ⚠️ **`ADX` has no direction** — it rises in a hard sell-off exactly as it does in a rally,
    because `DX` takes an absolute value. Reading it as bullish is the classic misuse; the
    direction is in the `+DI`/`-DI` pair, which is why they are components rather than internals.

    **Only one `-DM` or `+DM` can be non-zero on a bar.** An outside bar that made both a higher
    high and a lower low is not two units of directional movement — the larger move wins and the
    other is zero. An implementation that credits both reports a market as trending both ways at
    once, and `DX` (their difference over their sum) collapses toward zero exactly when the bar
    was most decisive.

    **True Range is the same definition `ATR` uses**, gaps included, and for the same reason.
    ⚠️ But unlike `ATR`, the first bar contributes **nothing**: `TR` alone could be seeded from
    `high - low`, while `+DM` needs a previous high to have moved from. Seeding `TR` a bar
    earlier than the `DM`s would put one more bar of range under a ratio whose numerator had not
    started counting, and every `DI` in the run would read slightly low.

    **Wilder smoothing throughout** (weight `1 / period`), written in the same stable increment
    form as `EMA`, `RSI` and `ATR`. Wilder's own notation accumulates sums rather than averages —
    `S = S_prev - S_prev/period + x` — which is this form scaled by `period`; the scale cancels
    in `+DM / TR`, so the `DI` values are identical either way.

    ⚠️ **The components warm up at different bars, and the gap is `period - 1`.** The `DM`/`TR`
    averages seed on bar `period`, so `+DI`/`-DI` answer from there. `ADX` is a *second*
    smoothing, seeded from the first `period` values of `DX`, so it answers only on bar
    `2·period - 1` — bar 27 for the standard period of 14. An implementation that reports `ADX`
    as soon as the `DI` pair exists is reporting a single `DX` under the name of an average, and
    it is the most common way an ADX disagrees with a chart at the start of a series.
    """

    COMPONENTS: Final = ("adx", "plus_di", "minus_di")

    def __init__(self, *, period: int) -> None:
        if period < 1:
            raise ValueError(f"ADX period must be >= 1, got {period}")
        super().__init__()
        self._period = period
        self._previous: Candle | None = None
        self._seed_count = 0
        self._seed_plus: Money = ZERO
        self._seed_minus: Money = ZERO
        self._seed_true_range: Money = ZERO
        self._average_plus: Money | None = None
        self._average_minus: Money | None = None
        self._average_true_range: Money | None = None
        self._dx_count = 0
        self._dx_sum: Money = ZERO
        self._adx: Money | None = None

    def _directional_movement(self, candle: Candle, previous: Candle) -> tuple[Money, Money]:
        up = candle.high - previous.high
        down = previous.low - candle.low
        # Strictly greater on both counts: an equal move is not a directional one, and two
        # non-zero DMs on one bar is the failure the class docstring describes.
        plus = up if up > down and up > ZERO else ZERO
        minus = down if down > up and down > ZERO else ZERO
        return plus, minus

    def update(self, candle: Candle) -> None:
        if self._is_repeat(candle):
            return
        previous = self._previous
        self._previous = candle
        if previous is None:
            # No prior bar to have moved from — see the docstring on why TR does not start here
            # either, though it could.
            return

        plus, minus = self._directional_movement(candle, previous)
        true_range = max(
            candle.high - candle.low,
            abs(candle.high - previous.close),
            abs(candle.low - previous.close),
        )

        # The three are asked about together rather than through `_average_true_range` alone:
        # they seed on the same bar, and testing all three is what lets the type checker know
        # the other branch has numbers — no assertion standing in for the invariant.
        if (
            self._average_plus is None
            or self._average_minus is None
            or self._average_true_range is None
        ):
            self._seed_count += 1
            self._seed_plus += plus
            self._seed_minus += minus
            self._seed_true_range += true_range
            if self._seed_count < self._period:
                return
            self._average_plus = self._seed_plus / self._period
            self._average_minus = self._seed_minus / self._period
            self._average_true_range = self._seed_true_range / self._period
        else:
            self._average_plus += (plus - self._average_plus) / self._period
            self._average_minus += (minus - self._average_minus) / self._period
            self._average_true_range += (true_range - self._average_true_range) / self._period

        # DX exists from the bar the averages seed on, including that bar — it is the first of
        # the `period` values the ADX seed is the mean of. The pair is computed from the three
        # averages directly rather than through `_di_pair`, whose `None` case cannot arise here:
        # every path above either returned or left all three set. Routing through it would add a
        # branch no test could reach, and an unreachable branch reads as a case somebody handled.
        dx = self._directional_index(
            *self._direction(self._average_plus, self._average_minus, self._average_true_range)
        )
        if self._adx is None:
            self._dx_count += 1
            self._dx_sum += dx
            if self._dx_count == self._period:
                self._adx = self._dx_sum / self._period
        else:
            self._adx += (dx - self._adx) / self._period

    def _direction(self, plus: Money, minus: Money, true_range: Money) -> tuple[Money, Money]:
        """The `+DI`/`-DI` pair from three smoothed averages that are known to exist."""
        if true_range == ZERO:
            # A window with no range at all: every bar an identical doji, no gaps. There is no
            # movement to apportion, so neither side is winning — 0, not a division by zero.
            return ZERO, ZERO
        scale = Decimal(100) / true_range
        return plus * scale, minus * scale

    def _di_pair(self) -> tuple[Money, Money] | None:
        """The same pair for a reader, `None` while the averages are still seeding."""
        if (
            self._average_true_range is None
            or self._average_plus is None
            or self._average_minus is None
        ):
            return None
        return self._direction(self._average_plus, self._average_minus, self._average_true_range)

    def _directional_index(self, plus: Money, minus: Money) -> Money:
        total = plus + minus
        if total == ZERO:
            # No directional movement either way. `DX` measures how one-sided the movement was;
            # with none to be one-sided about, the honest reading is zero, and it is what keeps
            # a flat stretch from raising instead of reporting "no trend".
            return ZERO
        return Decimal(100) * abs(plus - minus) / total

    def components(self) -> Mapping[str, Money | None]:
        pair = self._di_pair()
        plus, minus = (None, None) if pair is None else pair
        return {"adx": self._adx, "plus_di": plus, "minus_di": minus}


@dataclass(frozen=True, slots=True)
class ComponentView:
    """One component of a composite, wearing the single-valued `Indicator` shape.

    It exists for `Charted.overlays`, whose contract is a mapping of label to `Indicator` and
    whose reader drives what it is handed. Three views over one `Bollinger` let a chart draw
    three curves without the overlay path learning that composites exist — and the repeat guard
    in `_FoldsOnce` is what makes the three `update` calls per bar mean one bar.
    """

    composite: CompositeIndicator
    component: str

    def update(self, candle: Candle) -> None:
        self.composite.update(candle)

    def value(self) -> Money | None:
        return self.composite.components()[self.component]


# The registry. A DSL indicator names a `type`; this maps it to a constructor. Each indicator
# satisfies the `Indicator` Protocol structurally — none inherits anything — so the registry is
# typed against the Protocol, and a new indicator is genuinely one new class plus one line here.
def _params_of(spec: Mapping[str, object]) -> Mapping[str, object]:
    """The `params` block, or a sentence saying it is not one.

    ⚠️ `spec["params"]` used to be a bare subscript, so a document without the key left this module
    raising `KeyError: 'params'` — a traceback, against this file's own promise of a sentence.
    Reachable only by a mapping that did not come through the schema, which is exactly the case
    where the message has to carry its own context: the caller has no validator to consult.
    """
    params = spec.get("params")
    if not isinstance(params, Mapping):
        raise EngineError(f"indicator {spec.get('id')!r}: params must be an object, got {params!r}")
    return params


def _whole_number(spec: Mapping[str, object], params: Mapping[str, object], name: str) -> int:
    """Read one required integer parameter, refusing absence and nonsense in the same voice.

    `bool` is excluded even though it is an `int` subclass: a period of `true` is a mistake, not
    the number 1 — the same line `compile_constant` draws in `expressions`.
    """
    value = params.get(name)
    if value is None:
        raise EngineError(f"indicator {spec.get('id')!r}: params.{name} is required")
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise EngineError(
            f"indicator {spec.get('id')!r}: params.{name} must be a whole number, got {value!r}"
        )
    try:
        number = int(value)
    # ⚠️ `OverflowError` alongside `ValueError`: `int(float("inf"))` raises the first and not the
    # second, so an infinity used to escape as a raw traceback — the very thing this function was
    # written to stop. `nan` was only caught because `int(nan)` happens to raise `ValueError`.
    except (ValueError, OverflowError) as error:
        raise EngineError(
            f"indicator {spec.get('id')!r}: params.{name} must be a whole number, got {value!r}"
        ) from error
    # ⚠️ And a whole number means a whole number. `int(2.7)` is 2, so a period of 2.7 used to be
    # silently truncated by a function whose refusal message says "must be a whole number" — the
    # document would run a 2-period average while its author read 2.7 off the screen.
    if isinstance(value, float) and number != value:
        raise EngineError(
            f"indicator {spec.get('id')!r}: params.{name} must be a whole number, got {value!r}"
        )
    return number


def _period_source_builder(
    cls: Callable[..., Indicator],
) -> Callable[[Mapping[str, object]], Indicator]:
    def build(spec: Mapping[str, object]) -> Indicator:
        params = _params_of(spec)
        source = params.get("source", "close")
        return cls(period=_whole_number(spec, params, "period"), source=str(source))

    return build


def _period_builder(
    cls: Callable[..., Indicator],
) -> Callable[[Mapping[str, object]], Indicator]:
    """For the indicators that read the whole candle, so a price source has nothing to name."""

    def build(spec: Mapping[str, object]) -> Indicator:
        params = _params_of(spec)
        return cls(period=_whole_number(spec, params, "period"))

    return build


def _bollinger_builder(spec: Mapping[str, object]) -> CompositeIndicator:
    params = _params_of(spec)
    # Through `str`, never `float(...)`: a document asking for 2.5 deviations must widen the
    # band by exactly 2.5 standard deviations, not by the binary approximation (ADR-0011).
    try:
        deviations = Decimal(str(params.get("deviations", 2)))
    except InvalidOperation as error:
        raise EngineError(
            f"indicator {spec.get('id')!r}: params.deviations must be a number, "
            f"got {params.get('deviations')!r}"
        ) from error
    return Bollinger(
        period=_whole_number(spec, params, "period"),
        source=str(params.get("source", "close")),
        deviations=deviations,
    )


def _adx_builder(spec: Mapping[str, object]) -> CompositeIndicator:
    return ADX(period=_whole_number(spec, _params_of(spec), "period"))


INDICATOR_BUILDERS: Final[
    dict[str, Callable[[Mapping[str, object]], Indicator | CompositeIndicator]]
] = {
    "SMA": _period_source_builder(SMA),
    "EMA": _period_source_builder(EMA),
    "RSI": _period_source_builder(RSI),
    "ATR": _period_builder(ATR),
    "HIGHEST": _period_builder(Highest),
    "LOWEST": _period_builder(Lowest),
    "BOLLINGER": _bollinger_builder,
    "ADX": _adx_builder,
}

# The component names of every composite type, for the compiler to expand a declaration into
# one readable channel per component. ⚠️ Duplicated in `tradeforge_schema.models` — the two
# packages do not import each other — and pinned equal by a test in `apps/api`, which depends on
# both. Drift here is silent: the schema would accept `bb.uppper`, the engine would resolve it
# to `None`, and a comparison against `None` is false for ever.
COMPOSITE_COMPONENTS: Final[Mapping[str, tuple[str, ...]]] = {
    "BOLLINGER": Bollinger.COMPONENTS,
    "ADX": ADX.COMPONENTS,
}


def build_indicator(spec: Mapping[str, object]) -> tuple[str, Indicator | CompositeIndicator]:
    """Turn one DSL indicator spec into `(id, indicator)`.

    Raises rather than guess on a `type` the engine was not built for: a strategy naming an
    indicator the engine cannot compute must fail loudly at compile time, not run on a
    default and produce a plausible, wrong backtest.
    """
    indicator_type = spec.get("type")
    builder = INDICATOR_BUILDERS.get(str(indicator_type))
    if builder is None:
        raise EngineError(
            f"unknown indicator type {indicator_type!r}; "
            f"this engine builds {sorted(INDICATOR_BUILDERS)}"
        )
    indicator_id = spec.get("id")
    if not isinstance(indicator_id, str):
        raise EngineError(f"indicator spec is missing a string id: {spec!r}")
    return indicator_id, builder(spec)


__all__ = [
    "ADX",
    "ATR",
    "COMPOSITE_COMPONENTS",
    "EMA",
    "INDICATOR_BUILDERS",
    "RSI",
    "SMA",
    "Bollinger",
    "ComponentView",
    "Highest",
    "Lowest",
    "build_indicator",
]

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
from collections.abc import Callable, Mapping
from decimal import Decimal
from typing import Final

from tradeforge_engine.domain import ZERO, Candle, Money
from tradeforge_engine.errors import EngineError
from tradeforge_engine.protocols import Indicator

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


# The registry. A DSL indicator names a `type`; this maps it to a constructor. Each indicator
# satisfies the `Indicator` Protocol structurally — none inherits anything — so the registry is
# typed against the Protocol, and a new indicator is genuinely one new class plus one line here.
def _period_source_builder(
    cls: Callable[..., Indicator],
) -> Callable[[Mapping[str, object]], Indicator]:
    def build(spec: Mapping[str, object]) -> Indicator:
        params = spec["params"]
        if not isinstance(params, Mapping):
            raise EngineError(f"indicator {spec.get('id')!r}: params must be an object")
        period = params["period"]
        source = params.get("source", "close")
        return cls(period=int(period), source=str(source))

    return build


def _period_builder(
    cls: Callable[..., Indicator],
) -> Callable[[Mapping[str, object]], Indicator]:
    """For the indicators that read the whole candle, so a price source has nothing to name."""

    def build(spec: Mapping[str, object]) -> Indicator:
        params = spec["params"]
        if not isinstance(params, Mapping):
            raise EngineError(f"indicator {spec.get('id')!r}: params must be an object")
        return cls(period=int(params["period"]))

    return build


INDICATOR_BUILDERS: Final[dict[str, Callable[[Mapping[str, object]], Indicator]]] = {
    "SMA": _period_source_builder(SMA),
    "EMA": _period_source_builder(EMA),
    "RSI": _period_source_builder(RSI),
    "ATR": _period_builder(ATR),
    "HIGHEST": _period_builder(Highest),
    "LOWEST": _period_builder(Lowest),
}


def build_indicator(spec: Mapping[str, object]) -> tuple[str, Indicator]:
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
    "ATR",
    "EMA",
    "INDICATOR_BUILDERS",
    "RSI",
    "SMA",
    "Highest",
    "Lowest",
    "build_indicator",
]

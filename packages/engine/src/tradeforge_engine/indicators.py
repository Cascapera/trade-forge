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
        self._value = price * self._alpha + self._value * (Decimal(1) - self._alpha)

    def value(self) -> Money | None:
        return self._value


# The registry. A DSL indicator names a `type`; this maps it to a constructor. `Indicator`
# (the Protocol) is satisfied structurally — SMA and EMA inherit nothing.
def _build_moving_average(
    cls: type[SMA] | type[EMA],
) -> Callable[[Mapping[str, object]], SMA | EMA]:
    def build(spec: Mapping[str, object]) -> SMA | EMA:
        params = spec["params"]
        if not isinstance(params, Mapping):
            raise EngineError(f"indicator {spec.get('id')!r}: params must be an object")
        period = params["period"]
        source = params.get("source", "close")
        return cls(period=int(period), source=str(source))

    return build


INDICATOR_BUILDERS: Final[dict[str, Callable[[Mapping[str, object]], SMA | EMA]]] = {
    "SMA": _build_moving_average(SMA),
    "EMA": _build_moving_average(EMA),
}


def build_indicator(spec: Mapping[str, object]) -> tuple[str, SMA | EMA]:
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


__all__ = ["EMA", "INDICATOR_BUILDERS", "SMA", "build_indicator"]

"""How much a run reads before its window, and where that reading starts (ADR-0030, 28/09).

A run warms up on the bars before `date_from`, trading on them as shadow (the engine's
`book_from`), so that its averages and its structure are ready when the window opens. How many:

* **4 times the longest period the document reads** — an exponential average forgets its seed over
  about four periods; a long average of 200 wants 800 bars.
* **One calendar year for a structure setup** — structure has no period (ADR-0023 measured its
  first CHoCH anywhere between 38 and 730 bars), and the measurement of 28/09 found one year to be
  enough for the structure setups tested.

Whichever reaches further back. With less history than that before the window, the run warms on
what there is; the bars it actually read are recorded (`CandleWindow.warmed`), never assumed.

⚠️ **Two runs share a batch only if they warm up alike** (`batching.batch_key`): a batch feeds one
stream of bars to every member, and a member given more warm-up than it would take alone could
come out differently.
"""

import datetime as dt
from bisect import bisect_left
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from tradeforge_engine.domain import Candle

PERIOD_FACTOR = 4
"""Bars of warm-up per bar of the longest period."""

STRUCTURE_SPAN = dt.timedelta(days=365)
"""Warm-up of a structure setup, in calendar time."""

_PERIODS = frozenset({"fast", "slow", "signal"})
"""Keys that name a period without saying so (a MACD's); every key ending in `period` does."""


@dataclass(frozen=True, slots=True)
class WarmUp:
    """What a document needs read before its window: `bars`, and at least `span` of time."""

    bars: int
    span: dt.timedelta | None


def warmup_for(definition: Mapping[str, Any]) -> WarmUp:
    """The warm-up a stored document asks for — see the module's rules."""
    longest = max(_periods(definition), default=0)
    setup = definition.get("setup")
    kind = setup.get("type") if isinstance(setup, Mapping) else None
    structure = isinstance(kind, str) and kind.startswith("structure_")
    return WarmUp(bars=PERIOD_FACTOR * longest, span=STRUCTURE_SPAN if structure else None)


def warm_start(candles: Sequence[Candle], warmup: WarmUp, date_from: dt.datetime) -> int:
    """The index of the first bar to read: `warmup` before the first bar of the window, or the
    first bar there is. `candles` in time order, as the reader hands them."""
    opens = bisect_left(candles, date_from, key=lambda candle: candle.time)
    start = max(0, opens - warmup.bars)
    if warmup.span is not None:
        start = min(start, bisect_left(candles, date_from - warmup.span, key=_time))
    return start


def _time(candle: Candle) -> dt.datetime:
    return candle.time


def _periods(node: Any) -> list[int]:  # noqa: ANN401 — a JSON document, walked
    found: list[int] = []
    if isinstance(node, Mapping):
        for key, value in node.items():
            named = isinstance(key, str) and (key.endswith("period") or key in _PERIODS)
            if named and isinstance(value, int) and not isinstance(value, bool) and value > 0:
                found.append(value)
            else:
                found += _periods(value)
    elif isinstance(node, list):
        for value in node:
            found += _periods(value)
    return found


__all__ = ["PERIOD_FACTOR", "STRUCTURE_SPAN", "WarmUp", "warm_start", "warmup_for"]

"""Anchored VWAP: the volume-weighted average price since a chosen anchor.

Ordinary VWAP resets every session; the **anchored** version starts wherever you point it — and
in this method the anchor is the candle that marked the high or low of the move being watched (a
swing). From there it answers one question: what is the average price everyone who traded since
that moment paid? That average is where the players who started the move tend to defend it, and
both of the author's triggers are built on watching someone fail against it.

⚠️ **This used to say the trigger was price "grazing it and failing" — the *roçadinha* — and that
is wrong twice over.** He discarded that pattern outright when he dictated chapter 11.2, and the
two entries he kept read the other way round: price is *above* the line on the retake, and the
signal is the **seller** getting through it and failing. Left uncorrected the sentence would send
the next reader looking for a setup this engine does not have, which is what a docstring nobody
tests eventually does. See `vwap_setups.py` for the two that exist.

**A band, from three sources.** The same anchor and the same volume, priced three ways: `hlc3`
(the typical price, the central line), `high` (an upper line) and `low` (a lower line). Together
they bound a volume-weighted zone rather than a single line — and the outer lines are not
decoration: the *botinha* is that band's lower line under a demand region and its upper line over
a supply one, which is why this indicator draws three and not one.

**Volume.** `real_volume` is the exchange's traded quantity — present on B3, zero on decentralised
forex; `tick_volume` counts price changes and is always there. The default, `auto`, takes real
volume when the venue reports it and falls back to ticks when it does not, so the same indicator
works on a stock and on a currency.

**Determinism.** The `hlc3` division and the final `Σpv / Σv` both run in `update`/`value`, under
the engine's pinned context — never in `__init__`, so no precision leaks in from the ambient
process (the trap the EMA's lazy alpha also avoids). Re-anchor with `reset()`: it forgets
everything before the next candle, which is how a strategy moves the anchor to a fresh swing.
"""

from collections.abc import Callable
from typing import Final

from tradeforge_engine.domain import ZERO, Candle, Money
from tradeforge_engine.errors import EngineError

_VOLUME_SOURCES: Final = frozenset({"auto", "real", "tick"})


def _price_reader(source: str) -> Callable[[Candle], Money]:
    """Resolve the price a bar contributes. `hlc3` is the typical price; the rest read one field."""
    if source == "hlc3":
        return lambda candle: (candle.high + candle.low + candle.close) / 3
    if source in {"high", "low", "close", "open"}:

        def read(candle: Candle) -> Money:
            value: Money = getattr(candle, source)
            return value

        return read
    raise EngineError(
        f"unknown VWAP source {source!r}; expected 'hlc3', 'high', 'low', 'close' or 'open'"
    )


class AnchoredVWAP:
    """Cumulative volume-weighted average price from the first candle it is fed (the anchor).

    `value()` is `None` until it has seen volume — an anchor bar with no volume prices nothing.
    `reset()` re-anchors it to the next candle, the mechanism a setup uses to move the anchor onto
    a new swing high or low.
    """

    def __init__(self, *, source: str = "hlc3", volume: str = "auto") -> None:
        if volume not in _VOLUME_SOURCES:
            raise EngineError(f"unknown VWAP volume {volume!r}; expected {sorted(_VOLUME_SOURCES)}")
        self._read_price = _price_reader(source)
        self._volume = volume
        self._cum_pv: Money = ZERO
        self._cum_volume: int = 0

    def _bar_volume(self, candle: Candle) -> int:
        if self._volume == "real":
            return candle.real_volume
        if self._volume == "tick":
            return candle.tick_volume
        # auto: the exchange's real volume where it exists (B3), ticks where it does not (forex).
        return candle.real_volume or candle.tick_volume

    def update(self, candle: Candle) -> None:
        volume = self._bar_volume(candle)
        if volume <= 0:
            # A bar with no volume moves no average — and must not divide by zero.
            return
        self._cum_pv += self._read_price(candle) * volume
        self._cum_volume += volume

    def value(self) -> Money | None:
        if self._cum_volume == 0:
            return None
        return self._cum_pv / self._cum_volume

    def reset(self) -> None:
        """Drop the accumulation so the next candle becomes the new anchor."""
        self._cum_pv = ZERO
        self._cum_volume = 0


__all__ = ["AnchoredVWAP"]

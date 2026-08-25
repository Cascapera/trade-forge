"""Joining years of history on disk to a stream of bars that have not happened yet.

A paper session needs both. The Parquet the collector backfilled is what warms the strategy —
an EMA-20 on H4 that starts cold is blind for three days, and a blind session looks exactly like
one that found no setup (ADR-0023). The Redis stream is what it trades. This module is the seam,
and the seam has three hazards that are each invisible when got wrong.

**1. The order of operations.** The consumer group must exist *before* history is read off disk.
Create it afterwards and every bar that closed while Parquet was being read is gone — a hole in
the equity curve, with nothing anywhere saying a bar was skipped. `splice()` owns that ordering
so that no caller has to remember it.

**2. The overlap is real, and it raises.** A group created at `0` is offered the whole stream,
which overlaps whatever the Parquet already had. Handing the engine a bar it has already seen is
not a silent duplicate — `loop._reject_out_of_order` raises on it — so a session would simply
fail to start. The rule here is stronger and simpler than de-duplicating against the last
Parquet bar: **the spliced sequence is strictly increasing in time, end to end.** One predicate
covers the overlap, a re-delivered bar after a restart, and a publisher that ever went
backwards.

⚠️ Dropped bars are counted and logged, never silent. "The stream replayed the tail of my
history" and "the collector is publishing out of order" both arrive here as the same drop, and
only the count tells an operator which one is happening.

**3. The gap between the two, and where a session's life begins.** The live publisher writes to
Redis only — it does **not** write Parquet, which is filled by collection jobs — so the last bar
on disk can be days behind the last bar on the wire. Those in-between bars are history that
happens to arrive through the live path, and treating them as live would have a session replay
three days of market at full speed and record it as paper trading.

So the cut is time, not source: **a bar belongs to the warm-up if it had already closed when the
session opened.** `warmup()` yields those, ends, and `live()` continues from exactly where it
stopped.

**Nothing can be lost at the cut.** History, backlog and stream are chained into *one* iterator,
walked once. The bar that ends the warm-up is held, not dropped, and `live()` hands it over
before asking that same iterator for anything else. Where the cut falls — inside the Parquet,
inside the backlog, or at the first bar that ever arrives — changes nothing.
"""

import datetime as dt
import logging
from collections.abc import Callable, Generator, Iterable, Iterator
from itertools import chain
from typing import Protocol

from tradeforge_engine.domain import Candle

logger = logging.getLogger(__name__)

__all__ = ["BarSource", "SplicedCandles", "splice"]


class BarSource(Protocol):
    """The two halves of a live stream this module needs: what is waiting, and what comes next.

    A `Protocol` for the same reason the engine's seams are protocols — a test can hand over a
    small honest object instead of a mock. `CandleStream` satisfies it structurally, and
    `test_splice.py` proves that by assignment rather than by claiming it in a docstring: a
    protocol nothing is ever checked against is a description of an imaginary client.
    """

    def ensure_group(self) -> bool: ...

    def backlog(self) -> Iterator[Candle]: ...

    def candles(self) -> Iterator[Candle]: ...


class SplicedCandles:
    """History and the stream as one strictly increasing sequence, cut in two at an instant.

    Two generators over **one** underlying iterator. Drive `warmup()` to exhaustion, hand the
    strategy over to a fresh broker (`warmup.hand_over`), then drive `live()`. The other order,
    or interleaving the two, is refused rather than supported: the cut is *defined* by where the
    first generator stopped, so there is nothing coherent for the second to mean before then.
    """

    __slots__ = (
        "_bars",
        "_closed_by",
        "_handed_over",
        "_held",
        "_last_time",
        "_live_started",
        "_timeframe",
        "dropped",
        "warmed",
    )

    def __init__(
        self, bars: Iterable[Candle], *, timeframe: dt.timedelta, opened_at: dt.datetime
    ) -> None:
        if timeframe <= dt.timedelta(0):
            raise ValueError(f"timeframe must be positive, got {timeframe}")
        if opened_at.tzinfo is None:
            # A naive cut compared against a UTC bar raises `TypeError` deep inside the first
            # comparison, which surfaces as a crash in the middle of a warm-up rather than as
            # the configuration mistake it is.
            raise ValueError("opened_at must be timezone-aware")

        self._bars = iter(bars)
        self._timeframe = timeframe
        self._closed_by = opened_at
        self._held: Candle | None = None
        self._last_time: dt.datetime | None = None
        self._handed_over = False
        self._live_started = False

        self.warmed = 0
        """Bars the warm-up actually drove the strategy over. What a session records."""

        self.dropped = 0
        """Bars refused for not being strictly later than the one before. Expected to be small
        and non-zero: the stream's replay overlaps the Parquet tail by construction."""

    def warmup(self) -> Generator[Candle, None, None]:
        """Every bar that had already closed when the session opened, oldest first.

        Ends at the first bar that had not. That bar is **held**, not consumed — see `live()`.
        """
        for candle in self._bars:
            if not self._is_history(candle):
                self._held = candle
                break
            if self._admit(candle):
                self.warmed += 1
                yield candle
        self._handed_over = True

    def live(self) -> Iterator[Candle]:
        """The session's own bars: the one that ended the warm-up, then the stream, for ever.

        ⚠️ Yielding the held bar first is what makes the seam lossless. It was read to decide
        where the cut was, and a read bar that nobody yields is a bar that silently never
        happened — the same class of hole as creating the consumer group too late.

        ⚠️ Both refusals guard one hazard: **two generators pulling on the same iterator**.
        Started before the warm-up finished, or started twice, they take bars from each other
        and each sees part of the market — alternately, plausibly, and without raising. It is
        the shared-consumer-group failure from `candle_stream`, one layer up, and it has to be a
        refusal precisely because there is no output that would look wrong.

        **Refused here rather than inside the generator**, the same way `loop.iter_run`
        validates. A `raise` written in a generator body waits for the first `next()`, so a
        session that called this twice would get two apparently valid iterators and discover the
        problem — or not — on its first bar, hours later.
        """
        if not self._handed_over:
            raise RuntimeError("warmup() must be driven to completion before live()")
        if self._live_started:
            raise RuntimeError("live() is the session's only pass over these bars")
        self._live_started = True
        return self._live()

    def _live(self) -> Generator[Candle, None, None]:
        # No clearing of `_held`, and none is needed: `live()` refuses a second pass.
        #
        # ⚠️ **Calling `_admit` here is load-bearing; branching on its answer is not, and the
        # difference is worth stating.** The call advances the "latest seen" mark to the held
        # bar, without which a stream that replays it — the ordinary case when the cut falls
        # inside the Parquet — would hand the engine the same bar twice.
        #
        # The answer, though, is provably always `True`: the held bar is *not* history
        # (`held.time + timeframe > opened_at`) and every warmed bar *is*
        # (`warmed.time + timeframe <= opened_at`), so `held.time > warmed.time` by
        # construction. Two mutants survive on that fact — resetting `_last_time` here, and
        # ignoring this return value — and both are equivalent for as long as `_is_history` is
        # what defines the cut. Written as a condition rather than an assertion because the day
        # that stops being true, dropping the bar is the safe answer and raising is not.
        if self._held is not None and self._admit(self._held):
            yield self._held

        for candle in self._bars:
            if self._admit(candle):
                yield candle

    def _is_history(self, candle: Candle) -> bool:
        """Had this bar already closed when the session opened?

        ⚠️ `Candle.time` is the bar's **opening** instant, so the close is `time + timeframe`.
        Comparing `time` against the cut instead would call a bar history while it was still
        forming, and hand the engine an unfinished bar as settled fact.
        """
        return candle.time + self._timeframe <= self._closed_by

    def _admit(self, candle: Candle) -> bool:
        if self._last_time is not None and candle.time <= self._last_time:
            self.dropped += 1
            logger.info(
                "dropped a bar at %s: not later than %s (%d dropped so far)",
                candle.time.isoformat(),
                self._last_time.isoformat(),
                self.dropped,
            )
            return False
        self._last_time = candle.time
        return True


def splice(
    source: BarSource,
    *,
    history: Callable[[], Iterable[Candle]],
    timeframe: dt.timedelta,
    opened_at: dt.datetime,
) -> SplicedCandles:
    """Build the spliced sequence, **creating the consumer group before reading history**.

    ⚠️ That ordering is the whole reason this is a function and not a constructor call. Reading
    Parquet takes seconds to tens of seconds; a bar that closes in that window is on the stream
    but not on disk, and a group created afterwards at `$` has already missed it. Creating the
    group first means those bars are waiting in the backlog, where the cut will correctly file
    them as history.

    `history` is a **callable**, not an iterable, for exactly that reason: passing the already
    read list would put the read at the call site, before this function could sequence it.
    """
    source.ensure_group()
    return SplicedCandles(
        chain(history(), source.backlog(), source.candles()),
        timeframe=timeframe,
        opened_at=opened_at,
    )

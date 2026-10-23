"""Telling everyone else what the venue is holding, on a cadence the order loop does not have.

**Why a thread and not a line in `Service.run`.** The order loop only turns when an order
arrives. A snapshot published from inside it would be fresh on a busy afternoon and hours old on
a quiet one — and a quiet market is exactly when a session restarts and needs the answer. Worse,
it would be freshest precisely when nothing had changed and stalest when something might have.

That is the same structural mistake `Heartbeat` documents on the API side, arriving from the
other direction: there the loop was too *slow* because a bar is four hours; here it is too
*irregular* because an order is an event. Either way, **the cadence of a signal must not be
borrowed from the cadence of the work.**

⚠️ **What this proves, and what it does not.** A fresh snapshot says the executor was able to ask
MetaTrader that recently and this is what it was told. It does **not** say the executor is
processing orders — a wedged order loop would keep publishing a perfectly accurate snapshot. The
two questions have two answers on purpose.
"""

import datetime as dt
import logging
import threading
from collections.abc import Callable
from typing import Protocol

from redis.typing import EncodableT, KeyT

from tradeforge_executor.wire import (
    VENUE_STATE,
    VENUE_STATE_FRESH_FOR,
    HeldPosition,
    VenueState,
    venue_state_text,
)

logger = logging.getLogger(__name__)

__all__ = ["PUBLISH_EVERY", "PositionReader", "StateWriter", "VenueSnapshot"]

PUBLISH_EVERY = dt.timedelta(seconds=15)
"""A third of `VENUE_STATE_FRESH_FOR`, so one missed beat is not an outage and two are."""


class StateWriter(Protocol):
    """The one Redis call this module makes, spelled in redis-py's own vocabulary.

    ⚠️ A `Protocol`, and narrow: it says exactly what is required of the client, so a test double
    is a small honest object rather than a mock of a library. The names and types are redis-py's —
    renaming `ex` or narrowing `name` describes a client that does not exist, and every double
    written against it would type-check while the real thing did not.
    """

    def set(self, name: KeyT, value: EncodableT, ex: int | None = None) -> object: ...


class PositionReader(Protocol):
    """The one question this module asks the venue.

    ⚠️ **Narrower than `OrderGateway` on purpose**, and this is the `LedgerView` lesson again: a
    `Protocol` is a description of what the *caller* needs, not a re-export of what the
    implementation happens to offer. Typed against the whole gateway, every double written for
    this module would have to describe six methods it never calls — and a method nobody calls
    cannot be modelled wrongly, so describing it can only ever be a way of being wrong.

    `MT5Gateway` satisfies this structurally, with no registration and no inheritance.
    """

    def holdings(self) -> tuple[HeldPosition, ...]: ...


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


class VenueSnapshot:
    """Publishes what the venue holds, every `PUBLISH_EVERY`, until stopped."""

    def __init__(
        self,
        client: StateWriter,
        gateway: PositionReader,
        *,
        every: dt.timedelta = PUBLISH_EVERY,
        now: Callable[[], dt.datetime] = _utcnow,
    ) -> None:
        self._client = client
        self._gateway = gateway
        self._every = every
        self._now = now
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.published = 0

    def start(self) -> None:
        """Publish once, immediately, and then on the interval.

        ⚠️ **The first publish is at `start()`, not one interval later.** A session that comes up
        seconds after the executor would otherwise read an absent key and refuse to start — the
        right answer to "I do not know", and the wrong reason for it.
        """
        self.publish_once()
        thread = threading.Thread(target=self._loop, name="venue-snapshot", daemon=True)
        self._thread = thread
        thread.start()

    def stop(self, *, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None

    def _loop(self) -> None:
        while not self._stop.wait(self._every.total_seconds()):
            self.publish_once()

    def publish_once(self) -> bool:
        """One snapshot. `False` if the venue could not be asked — and then **nothing is written**.

        ⚠️ **A terminal that cannot be read must not produce an empty snapshot.** "I asked and the
        account holds nothing" and "I could not ask" are different facts, and only one of them
        lets a session start. Writing an empty document on failure would turn the second into the
        first, silently, at the exact moment the answer matters — so the old key is left to go
        stale instead, and stale reads as "I do not know".
        """
        try:
            positions = self._gateway.holdings()
        except Exception:
            logger.exception("could not read the venue; leaving the last snapshot to go stale")
            return False

        state = VenueState(at=self._now(), positions=positions)
        try:
            # ⚠️ The TTL is belt to the timestamp's braces. The timestamp lets a reader say *how*
            # stale, which the refusal message needs; the TTL means a dead executor's key
            # eventually disappears rather than lingering as a plausible-looking answer about an
            # account nobody is watching any more.
            self._client.set(
                VENUE_STATE,
                venue_state_text(state),
                ex=int(VENUE_STATE_FRESH_FOR.total_seconds()) * 2,
            )
        except Exception:
            logger.exception("could not publish the venue snapshot")
            return False

        self.published += 1
        return True

    def __enter__(self) -> "VenueSnapshot":
        self.start()
        return self

    def __exit__(self, *_: object) -> None:
        self.stop()

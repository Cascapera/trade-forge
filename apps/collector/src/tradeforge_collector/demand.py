"""Which live pairs somebody wants right now, per broker (ADR-0032, signals PR 3).

A session — paper, live, or the coming SIGNAL mode — asks for its (symbol, timeframe) by writing
it here, and keeps asking: the ask is a **lease** that lapses unless renewed. One broker's live
loop reads its own set every round and watches exactly what is wanted. A session that dies stops
renewing and its pair drops out on its own, so nothing polls a market for nobody.

One sorted set per broker, `live:wanted:<slug>`, member `SYMBOL|TF`, score = the instant the lease
lapses. A sorted set rather than a key per pair because "what is still wanted" is then one range
query, and "drop what lapsed" one more, with no `SCAN` over the keyspace.

⚠️ The symbol is the **internal** name (`WIN`, not `WIN$`): it names the stream a session reads,
`candles.WIN.M15`. The loop translates it to the broker's ticker when it asks the terminal.
"""

import logging
import threading
import time
from collections.abc import Callable
from typing import Any, Protocol

from tradeforge_collector.live import Subscription

__all__ = [
    "DEFAULT_LEASE",
    "DemandLease",
    "DemandStore",
    "demand_key",
    "release",
    "want",
    "wanted",
]

logger = logging.getLogger(__name__)

DEFAULT_LEASE = 90.0
"""Seconds an ask lasts. Renewed every third of that, so two missed renewals still hold it."""


class DemandStore(Protocol):
    """The slice of a Redis client the demand needs — real Redis, or a fake in tests."""

    def zadd(self, name: str, mapping: dict[str, float]) -> object: ...

    def zrem(self, name: str, *values: str) -> object: ...

    def zremrangebyscore(self, name: str, low: float | str, high: float | str, /) -> object: ...

    # `Any`: redis-py types every reply as `Awaitable[Any] | Any`, sync client or not.
    def zrange(self, name: str, start: int, end: int) -> Any: ...  # noqa: ANN401


def demand_key(broker: str) -> str:
    """`live:wanted:<slug>` — one broker's asks."""
    return f"live:wanted:{broker}"


def _member(subscription: Subscription) -> str:
    return f"{subscription.symbol}|{subscription.timeframe}"


def want(
    store: DemandStore,
    broker: str,
    subscription: Subscription,
    *,
    lease: float = DEFAULT_LEASE,
    now: Callable[[], float] = time.time,
) -> None:
    """Ask `broker`'s live loop for this pair until `lease` seconds from now."""
    store.zadd(demand_key(broker), {_member(subscription): now() + lease})


def release(store: DemandStore, broker: str, subscription: Subscription) -> None:
    """Withdraw the ask at once — a session ending cleanly, rather than waiting out its lease.

    ⚠️ Two sessions on the same pair share one member, so the first to end withdraws the other's
    ask too; the survivor's next renewal (a third of a lease later) puts it back, and the loop
    keeps polling in between because it only drops a pair on its next read of the set.
    """
    store.zrem(demand_key(broker), _member(subscription))


def wanted(
    store: DemandStore, broker: str, *, now: Callable[[], float] = time.time
) -> list[Subscription]:
    """Every pair still leased for `broker`, after dropping the lapsed ones, in name order."""
    key = demand_key(broker)
    store.zremrangebyscore(key, "-inf", now())
    found = []
    for raw in store.zrange(key, 0, -1):
        member = raw.decode() if isinstance(raw, bytes) else raw
        symbol, _, timeframe = member.partition("|")
        if symbol and timeframe:
            found.append(Subscription(symbol=symbol, timeframe=timeframe))
    return sorted(found, key=lambda one: (one.symbol, one.timeframe))


class DemandLease:
    """Keeps one pair asked for while a session runs: renews on a daemon thread, releases on stop.

    A thread rather than a renewal inside the session's bar loop, because that loop blocks on the
    stream for as long as a bar takes — four hours on H4 — and the lease is ninety seconds.
    """

    def __init__(
        self,
        store: DemandStore,
        broker: str,
        subscription: Subscription,
        *,
        lease: float = DEFAULT_LEASE,
    ) -> None:
        self._store = store
        self._broker = broker
        self._subscription = subscription
        self._lease = lease
        self._stopped = threading.Event()
        self._thread = threading.Thread(target=self._renew, name="live-demand", daemon=True)

    def start(self) -> "DemandLease":
        """Ask now — before returning, so the loop can start polling — then keep asking."""
        want(self._store, self._broker, self._subscription, lease=self._lease)
        self._thread.start()
        return self

    def stop(self) -> None:
        """Stop renewing and withdraw the ask."""
        self._stopped.set()
        self._thread.join(timeout=5)
        try:
            release(self._store, self._broker, self._subscription)
        except Exception:  # the lease lapses on its own; a failed release must not mask the exit
            logger.warning("could not withdraw the live ask; it lapses by itself", exc_info=True)

    def _renew(self) -> None:
        while not self._stopped.wait(self._lease / 3):
            try:
                want(self._store, self._broker, self._subscription, lease=self._lease)
            except Exception:  # Redis away for a moment; the next renewal tries again
                logger.warning("could not renew the live ask", exc_info=True)

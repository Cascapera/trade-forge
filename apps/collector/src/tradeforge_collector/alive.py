"""The host agent says it is running: a Redis key refreshed every half minute, gone within 90 s.

The API and the backtest worker ask this key one question — *is anybody there to collect?* —
before planning a download and while a run waits for one. Without it, a download queued while
the agent is off sits `queued` for ever, and the run behind it waited two hours to fail even
with years of history on disk (26/09).

⚠️ **A thread, not an arq job or arq's own health check.** `collect_range` is synchronous and
holds the event loop for as long as a year of M1 takes to download; anything scheduled on that
loop — arq's `health_check_interval` included — goes silent exactly while the agent is busiest,
and a busy agent would read as a dead one. A daemon thread with its own connection keeps
beating through a download, and dies with the process, which is the one thing the key must
never outlive by more than its expiry.

⚠️ **Expiry, not deletion, is what reports a death.** A process killed from the task manager
runs no shutdown hook; the key simply stops being renewed and Redis drops it after `TTL`.
The shutdown hook deletes it too, so a clean stop is seen at once rather than 90 s later.
"""

import datetime as dt
import logging
import threading

import redis

logger = logging.getLogger(__name__)

ALIVE_KEY = "collector:alive"
"""⚠️ Read by `tradeforge_api.collector`, which imports it rather than spelling it again."""

TTL_SECONDS = 90
"""How long the key outlives its last renewal: three beats, so one missed beat is not a death."""

EVERY_SECONDS = 30


class Heartbeat:
    """Renews `ALIVE_KEY` — and the agent's own broker's key, when it serves one — from a daemon
    thread until `stop()`.

    ⚠️ With several agents (ADR-0032) the shared key says *somebody* is there to collect, which is
    the question the API and the worker ask; `collector:alive:<broker>` says *which*. A clean stop
    clears only its own broker's key: another agent may still be renewing the shared one.
    """

    def __init__(
        self, client: redis.Redis, *, every: float = EVERY_SECONDS, broker: str | None = None
    ) -> None:
        self._client = client
        self._keys = (ALIVE_KEY,) if broker is None else (ALIVE_KEY, alive_key(broker))
        self._own = ALIVE_KEY if broker is None else alive_key(broker)
        self._every = every
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="collector-alive", daemon=True)

    def start(self) -> None:
        self.beat()
        self._thread.start()

    def beat(self) -> None:
        """One renewal. A Redis that does not answer is logged and tried again next beat —
        the agent's own watchdog (`supervisor`) is what reacts to Redis being gone."""
        now = dt.datetime.now(tz=dt.UTC).isoformat()
        try:
            for key in self._keys:
                self._client.set(key, now, ex=TTL_SECONDS)
        except redis.RedisError as error:
            logger.warning("could not renew %s: %s", ", ".join(self._keys), error)

    def stop(self) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=self._every)
        try:
            self._client.delete(self._own)
        except redis.RedisError as error:
            logger.warning("could not clear %s: %s", self._own, error)

    def _run(self) -> None:
        while not self._stop.wait(self._every):
            self.beat()


def alive_key(broker: str) -> str:
    """The key one broker's agent renews beside the shared one."""
    return f"{ALIVE_KEY}:{broker}"


__all__ = ["ALIVE_KEY", "EVERY_SECONDS", "TTL_SECONDS", "Heartbeat", "alive_key"]

"""Is the host agent running? — asked before a download is planned and while a run waits for one.

The agent renews `ALIVE_KEY` every 30 s and lets it expire 90 s after its last renewal
(`tradeforge_collector.alive`). This module only reads it.

⚠️ **Why the answer changes what a launch does** (his rule of 26/09: every launch collects what
is missing by itself, and runs on what is on disk when it cannot). A download queued while the
agent is off is never picked up: its row stays `queued`, and every run linked to it used to wait
two hours and then fail — with years of history on disk it could have read. Asked first, an
agent that is off means *nothing is planned*: the launch runs over what the dataset holds, as if
it had been told not to collect.

⚠️ **A Redis that cannot be asked reads as "off".** The launch that follows would fail to
enqueue anyway; and a wait that cannot tell whether the agent is there should not go on waiting.
"""

import logging
from collections.abc import Awaitable
from typing import Protocol

from redis import RedisError
from redis.typing import KeyT, ResponseT

# ⚠️ Imported, never spelled again — the kill switch's rule: a copy agrees the day it is written
# and disagrees, silently, the day one side changes; every launch would then read "off".
from tradeforge_collector.alive import ALIVE_KEY

logger = logging.getLogger(__name__)


class AliveStore(Protocol):
    """What reading the key needs: a synchronous `exists`, as `redis.Redis` has."""

    def exists(self, *names: KeyT) -> ResponseT: ...


class Collector:
    """The host agent as the API sees it: running or not, and nothing more."""

    def __init__(self, store: AliveStore) -> None:
        self._store = store

    def alive(self) -> bool:
        try:
            return bool(self._store.exists(ALIVE_KEY))
        except RedisError as error:
            logger.warning("could not ask whether the collector is running: %s", error)
            return False


class AsyncAliveStore(Protocol):
    """The same question on an asyncio client — the worker's arq pool."""

    def exists(self, *names: KeyT) -> Awaitable[int]: ...


async def running(store: AsyncAliveStore) -> bool:
    """`Collector.alive` for the worker, which holds an asyncio client and no sync one."""
    try:
        return bool(await store.exists(ALIVE_KEY))
    except RedisError as error:
        logger.warning("could not ask whether the collector is running: %s", error)
        return False


__all__ = ["ALIVE_KEY", "AliveStore", "AsyncAliveStore", "Collector", "running"]

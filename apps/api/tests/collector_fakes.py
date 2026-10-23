"""The host agent as a test wants it: running, or not.

⚠️ **Passed to every app that launches with `collect_missing`.** Without one the app builds a
`Collector` over the real Redis, where no agent writes its key — so every launch would read the
agent as off, plan nothing, and a test of "the download is queued" would test the fallback.
"""

from redis.typing import KeyT

from tradeforge_api.collector import Collector


class _Key:
    def __init__(self, *, present: bool) -> None:
        self._present = present

    def exists(self, *names: KeyT) -> int:
        return len(names) if self._present else 0


def running() -> Collector:
    return Collector(_Key(present=True))


def stopped() -> Collector:
    return Collector(_Key(present=False))


class AsyncKey:
    """`collector.running`'s side: the worker asks on its asyncio client."""

    def __init__(self, *, present: bool) -> None:
        self._present = present

    async def exists(self, *names: KeyT) -> int:
        return len(names) if self._present else 0

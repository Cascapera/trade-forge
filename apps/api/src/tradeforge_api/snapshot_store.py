"""Where the heavy pages' snapshots are kept (05/10): the best-by-market maps, in Redis.

A sweep's summary is kept on its own row (`sweeps.summary`); the maps span every sweep, so they live
under keys of their own — one per metric and per "every run" — with the fingerprint of the runs
they were computed from, which says whether computing them again would change anything
(`snapshots.refresh_best`). Redis keeps them on disk (`--appendonly yes`): a restart serves the
last map instead of computing one on the first opening.
"""

from typing import Protocol

from tradeforge_api.best import BestMetric
from tradeforge_api.schemas import BestMapOut

_PREFIX = "tradeforge:snapshot:best"


class KeyValue(Protocol):
    """What the store needs of a Redis client — the real one, or a test's dict."""

    def get(self, name: str) -> object: ...

    def set(self, name: str, value: str) -> object: ...


class InMemory:
    """A dict that speaks `KeyValue` — the store of an app whose database was handed to it."""

    def __init__(self) -> None:
        self._values: dict[str, str] = {}

    def get(self, name: str) -> str | None:
        return self._values.get(name)

    def set(self, name: str, value: str) -> None:
        self._values[name] = value


def _key(metric: BestMetric, *, every_run: bool) -> str:
    return f"{_PREFIX}:{metric.value}:{'every' if every_run else 'floor'}"


class SnapshotStore:
    """The kept maps, and the fingerprint of the runs the last refresh read."""

    def __init__(self, client: KeyValue) -> None:
        self._client = client

    def read_best(self, metric: BestMetric, *, every_run: bool) -> BestMapOut | None:
        raw = self._client.get(_key(metric, every_run=every_run))
        return None if raw is None else BestMapOut.model_validate_json(str(raw))

    def keep_best(self, made: BestMapOut) -> None:
        self._client.set(_key(made.metric, every_run=made.every_run), made.model_dump_json())

    def best_fingerprint(self) -> str | None:
        raw = self._client.get(f"{_PREFIX}:fingerprint")
        return None if raw is None else str(raw)

    def keep_best_fingerprint(self, fingerprint: str) -> None:
        self._client.set(f"{_PREFIX}:fingerprint", fingerprint)


__all__ = ["InMemory", "KeyValue", "SnapshotStore"]

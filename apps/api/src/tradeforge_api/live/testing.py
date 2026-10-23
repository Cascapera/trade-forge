"""A Redis consumer group, in memory, for driving `CandleStream` without a server.

Shipped with the package rather than hidden in one test file, for the reason
`tradeforge_engine.testing` gives: two suites need it — the consumer's own unit tests and the
end-to-end acceptance — and a helper two suites copy is a helper that drifts into two versions.
The project's `--import-mode=importlib` makes that concrete rather than theoretical: test
modules here are not importable by name at all, so the only place a shared double *can* live is
beside the code it doubles.

⚠️ **This is a description of Redis written by the author of the code it feeds**, so it agrees
with `candle_stream.py` by construction — including anywhere both are wrong about what a real
server does. It can prove that module's logic and nothing about the protocol. That is what
`test_candle_stream_integration.py` is for, and why that file is not optional.
"""

from redis.exceptions import ResponseError
from redis.typing import GroupT, KeyT, StreamIdT

from tradeforge_collector.live import Subscription, stream_name
from tradeforge_collector.publisher import candle_fields, entry_id
from tradeforge_engine.domain import Candle

__all__ = ["EMPTY_READ_LIMIT", "FakeRedisStreams", "published"]


def _text(value: object) -> str:
    """Whatever redis-py's aliases allow, as the `str` this double keeps its books in."""
    return value.decode() if isinstance(value, bytes) else str(value)


# How many empty blocking reads the double tolerates before calling the scenario broken. Real
# Redis waits here and so does the production loop — for ever, and correctly. A test that
# reaches that state by mistake would hang the suite instead of failing it, and a hung run is
# an unreadable CI job rather than a red one.
EMPTY_READ_LIMIT = 20


def published(subscription: Subscription, *candles: Candle) -> list[tuple[str, dict[str, str]]]:
    """Entries exactly as `RedisCandlePublisher` would have written them.

    Built with the publisher's own `candle_fields` and `entry_id` rather than by hand, so a
    test asserting the round trip is asserting the real encoding. A hand-written fixture would
    keep passing after the wire format changed under it — which is the one thing a round-trip
    test exists to catch.
    """
    return [
        (entry_id(candle), {str(k): str(v) for k, v in candle_fields(subscription, candle).items()})
        for candle in candles
    ]


class FakeRedisStreams:
    """One stream and its consumer groups, modelling only what `CandleStream` leans on.

    Entries are ordered; `>` hands out what the group has never been delivered; `0` hands back
    this consumer's pending list; `XACK` is what takes an entry off it. Everything else Redis
    does is absent rather than stubbed — a method nobody calls cannot be modelled wrongly.
    """

    def __init__(
        self, subscription: Subscription, entries: list[tuple[str, dict[str, str]]] | None = None
    ) -> None:
        self.stream = stream_name(subscription)
        self.entries = list(entries or [])
        self.groups: dict[str, int] = {}
        self.pending: dict[str, list[str]] = {}
        self.acked: list[str] = []
        self.blocked_on: set[str] = set()
        self.empty_reads = 0

    def xgroup_create(
        self,
        name: KeyT,  # noqa: ARG002 — one stream per double; it knows its own name
        groupname: GroupT,
        id: StreamIdT,  # noqa: A002 — redis-py's own parameter name
        mkstream: bool,  # noqa: ARG002 — accepted so the signature matches the client
    ) -> object:
        group = _text(groupname)
        if group in self.groups:
            raise ResponseError("BUSYGROUP Consumer Group name already exists")
        # `$` starts after everything present; `0` before it. The only two `CandleStream`
        # offers, and the difference between them is the warm-up question it documents.
        self.groups[group] = len(self.entries) if _text(id) == "$" else 0
        return True

    def xreadgroup(
        self,
        groupname: str,
        consumername: str,
        streams: dict[KeyT, StreamIdT],
        count: int | None = None,  # noqa: ARG002 — unused, and a default the client also has
        block: int | None = None,
    ) -> object:
        # The client's types admit bytes and memoryview; the double keeps its books in `str`.
        # Narrowed once, here at the boundary, rather than at each of the six uses below.
        cursor = _text(streams[self.stream])
        if block is not None:
            self.blocked_on.add(cursor)

        if cursor == "0":
            waiting = self.pending.get(consumername, [])
            found = [entry for entry in self.entries if entry[0] in waiting]
            return [(self.stream, found)] if found else []

        position = self.groups[groupname]
        if position >= len(self.entries):
            self.empty_reads += 1
            if self.empty_reads > EMPTY_READ_LIMIT:
                raise AssertionError(
                    f"the stream blocked {EMPTY_READ_LIMIT} times with nothing to deliver: "
                    "the scenario is waiting for a bar that was never going to arrive"
                )
            return []

        self.empty_reads = 0
        fresh = self.entries[position:]
        self.groups[groupname] = len(self.entries)
        self.pending.setdefault(consumername, []).extend(entry[0] for entry in fresh)
        return [(self.stream, fresh)]

    def xack(
        self,
        name: KeyT,  # noqa: ARG002 — see xgroup_create
        groupname: GroupT,  # noqa: ARG002 — a pending list is per consumer, which is enough
        *ids: StreamIdT,
    ) -> object:
        for raw in ids:
            entry_ = _text(raw)
            self.acked.append(entry_)
            for waiting in self.pending.values():
                if entry_ in waiting:
                    waiting.remove(entry_)
        return len(ids)

"""Reading closed candles off the stream the collector writes, as an `Iterable[Candle]`.

The other end of `tradeforge_collector.publisher`. That module's job was to make a duplicate
impossible; this one's is to make a *miss* impossible — a session that was restarting when a
bar closed must find that bar waiting for it, not discover it is a bar short with nothing to
show for it.

**The wire format is imported, not re-implemented.** `stream_name` and `candle_from_fields`
come from the collector package that writes them. A second copy of "the field is called
`tick_volume` and prices are decimal text" would agree with the first on the day it was written
and disagree on the day one of them changed — and the disagreement would arrive as a paper
session quietly pricing something wrong, not as an error.

**One consumer group per session, not one per stream.** This is the part of Redis streams that
is easy to get backwards. A consumer group *divides* a stream between its consumers: two
readers in the same group see half the bars each. That is right for a work queue and
catastrophic here — two paper sessions trading EURUSD H1 must each see every bar, and the
failure mode of getting it wrong is not a crash, it is two strategies each trading half a
market. So the group name is the session's, and a session is alone in its own group.
"""

import logging
from collections.abc import Generator, Sequence
from typing import Protocol, cast

from redis.exceptions import ResponseError
from redis.typing import GroupT, KeyT, StreamIdT

from tradeforge_collector.live import Subscription, stream_name
from tradeforge_collector.publisher import candle_from_fields
from tradeforge_engine.domain import Candle

logger = logging.getLogger(__name__)

__all__ = ["CandleStream", "StreamReader"]

# How long a blocking read waits before coming back empty-handed. Not a deadline: an empty
# answer simply means no bar closed, and the loop below asks again. It exists so that a
# stopped session notices it was stopped within a minute rather than at the next bar, which
# on H4 would be four hours away.
DEFAULT_BLOCK_MS = 60_000

# Delivered but never confirmed — see `CandleStream.candles`.
_PENDING = "0"
_NEW = ">"


class StreamReader(Protocol):
    """The three Redis calls this module makes, and no more.

    A `Protocol` rather than `redis.Redis` for the reason the engine's five seams are
    protocols: it says exactly what is required of the client, so a test double is a small
    honest object instead of a mock of a library. `redis.Redis` satisfies it structurally,
    with no registration and no inheritance.

    ⚠️ A double built against this **cannot** prove that real Redis behaves this way — it can
    only prove this module behaves consistently with whatever the double believes. That is why
    `test_candle_stream_integration.py` exists and is not optional.

    **Spelled in redis-py's own type aliases, not in `str`.** `dict` is invariant, so a
    `streams: dict[str, str]` here is *not* satisfied by a client whose parameter is
    `dict[KeyT, StreamIdT]` — the protocol would be describing a client that does not exist,
    and every double written against it would type-check while the real thing did not. That is
    not a guess: `test_the_real_redis_client_satisfies_the_protocol` is what caught it. Same
    lesson `publisher.candle_fields` records on the other end of this stream — at the boundary,
    use the client's vocabulary; the tidier type is the wrong one.
    """

    # `id` shadows the builtin, and it is redis-py's parameter name — a `Protocol` that
    # renamed it would simply not match the client it exists to describe.
    def xgroup_create(
        self,
        name: KeyT,
        groupname: GroupT,
        id: StreamIdT,  # noqa: A002
        mkstream: bool,
    ) -> object: ...

    # ⚠️ `groupname` and `consumername` are `str` here while `xgroup_create` above takes the
    # wider aliases, and that asymmetry is redis-py's, not a slip: `xreadgroup` declares plain
    # `str` for both. A protocol is satisfied by a *wider* parameter, never a narrower one, so
    # copying `GroupT` down here would make the real client fail to match — which is exactly
    # what it did, until `test_the_real_redis_client_satisfies_the_protocol` said so.
    def xreadgroup(
        self,
        groupname: str,
        consumername: str,
        streams: dict[KeyT, StreamIdT],
        count: int | None = None,
        block: int | None = None,
    ) -> object: ...

    def xack(self, name: KeyT, groupname: GroupT, *ids: StreamIdT) -> object: ...


class CandleStream:
    """Closed candles for one subscription, in order, blocking until the next one exists.

    Hand `candles()` straight to `loop.iter_run` as its `candles` argument. That is the whole
    interface, and the engine cannot tell it from a cursor over ten years of history.

    Typed as a `Generator` rather than an `Iterator` so that `close()` is part of the contract:
    a live stream is stopped, not exhausted, and a caller that wants to stop one needs a way to
    say so. `Generator` is an `Iterator`, so the engine is none the wiser.
    """

    def __init__(  # noqa: PLR0913 — one client, one subscription, and four knobs, keyword-only
        self,
        client: StreamReader,
        subscription: Subscription,
        *,
        group: str,
        consumer: str = "session",
        block_ms: int = DEFAULT_BLOCK_MS,
        start_id: str = "$",
    ) -> None:
        """`start_id` is where a *brand new* group begins reading, and `$` means "from now".

        ⚠️ **`$` means the strategy starts cold**, and on a strategy with a warm-up that is not
        a detail. An EMA-20 reads `None` for its first twenty bars, so a session started at `$`
        on H4 is blind for three days and takes no trade it should have taken — while looking
        exactly like a session that simply found no setup. Passing `0` instead replays whatever
        the stream still holds (the publisher keeps roughly a week of M1), which warms the
        indicators at the cost of the session "trading" bars that are already history.

        Neither is right in general, so neither is chosen here. Seeding a session's warm-up
        from the *database* — where the collector's backfill already put years of bars — is the
        honest answer, and it belongs with the rest of session start-up in PR-302-B.

        The id only applies when the group is created. A group that already exists remembers
        its own position, and that is the point of it: it is what a restarted session resumes
        from.
        """
        self._client = client
        self._subscription = subscription
        self._stream = stream_name(subscription)
        self._group = group
        self._consumer = consumer
        self._block_ms = block_ms
        self._start_id = start_id

    def ensure_group(self) -> bool:
        """Create the consumer group if it is not there. `True` if this call created it.

        `mkstream` because a session may legitimately start before the collector has published
        anything for that symbol — the alternative is a session that refuses to start on a
        quiet market and starts fine an hour later, which is a race dressed up as a
        configuration error.

        The already-exists case is read from the error rather than pre-empted with a check, for
        the same reason the publisher reads its duplicate refusal that way: asking and then
        writing is two round trips with a race between them.
        """
        try:
            self._client.xgroup_create(
                name=self._stream, groupname=self._group, id=self._start_id, mkstream=True
            )
        except ResponseError as error:
            # ⚠️ Matched on the message because redis-py raises one type for every server-side
            # error. Anything else is re-raised: a key that has been turned into another type
            # must not read as "the group was already there".
            if "BUSYGROUP" in str(error).upper():
                return False
            raise
        return True

    def backlog(self) -> Generator[Candle, None, None]:
        """Every bar already waiting for this consumer, oldest first, and then **stop**.

        The difference from `candles()` is the ending. This one has one: it reads without
        blocking and returns the moment a read comes back empty, so a caller can ask "what have
        I missed?" and get an answer now rather than at the next bar.

        **That question is what a session start-up is made of.** A group created at `0` is
        offered the whole stream, and the bars in it that closed before the session opened are
        history — they belong to the warm-up, not to the session's ledger. Telling the two apart
        means reading them, and reading them must not stall: on H4, a blocking read with nothing
        left to hand over would keep a session in warm-up for four hours before it ever wrote a
        row saying it existed.

        Measured against a real server rather than assumed, because the two halves of that are
        not obvious from the docs: a group created at `0` hands back the *entire* backlog in a
        single read and does not block while it has one (5000 entries came back in one answer,
        in 0 ms, even with `BLOCK 60000`), and a read with no `BLOCK` on a drained stream returns
        empty immediately.

        Acknowledgement is lazy in exactly the sense `candles()` means it: a bar is confirmed
        when the consumer comes back for the next one. Driving this to exhaustion *is* coming
        back for a next one — the generator resumes, acks, and only then finds nothing left — so
        a fully drained backlog leaves nothing pending. A consumer that **abandons** the drain
        part way leaves the bar it was holding unconfirmed, which is the point: it may have died
        holding it, and Redis must offer it again.
        """
        # Called here as well as in `candles()`, so the method stands on its own. A session
        # start-up creates the group *earlier* still — before it reads history off disk — and
        # that ordering is the splice's to own, not this method's. See `live.splice`.
        self.ensure_group()

        cursor = _PENDING
        while True:
            entries = self._read(cursor, blocking=False)

            if not entries:
                if cursor == _PENDING:
                    # The pending list is drained; there may still be new bars behind it.
                    cursor = _NEW
                    continue
                return

            for entry_id, fields in entries:
                yield candle_from_fields(fields)
                self._client.xack(self._stream, self._group, entry_id)

    def candles(self) -> Generator[Candle, None, None]:
        """Every closed candle for this subscription, oldest first, for ever.

        **Acknowledgement is lazy, and that is the crash-safety.** A bar is `XACK`ed only when
        the consumer comes back for the *next* one — because a generator resumes after its
        `yield` exactly when the consumer has finished with what it was given. Acking on
        delivery instead would mean a session that died between receiving a bar and writing
        down what it did with it has told Redis it handled a bar it did not handle, and Redis
        would never offer it again. The bar would be gone, silently, and the equity curve would
        simply have a hole in it.

        The cost of the choice is the mirror image: a session that dies *after* processing a
        bar and before asking for the next one will be offered that bar again on restart. At
        least once, never at most once. Which of the two to pay for is not a matter of taste —
        a bar seen twice is a discrepancy something can notice, and a bar never seen is not.

        ⚠️ **The redelivery is not yet reconciled against anything.** This is PR-302-A: there
        is no `live_sessions` row and nothing persisted, so a restarted session rebuilds its
        broker and strategy from zero and replaying the pending bars is what it wants. Once a
        session has state on disk (PR-302-B), start-up has to compare the two — and until it
        does, a restart mid-bar is a session that re-runs a bar it already ran.
        """
        self.ensure_group()

        # First the backlog this consumer was handed and never confirmed, then the new bars.
        # `>` means "entries never delivered to this group"; `0` means "what is on my pending
        # list", which after a clean start is nothing and after a crash is the work in flight.
        cursor = _PENDING

        while True:
            entries = self._read(cursor)

            if not entries:
                # A pending list that came back empty is drained — switch to new bars, and do
                # not block on the way: there may be a backlog waiting behind it.
                if cursor == _PENDING:
                    cursor = _NEW
                    continue
                # No bar closed within the block. Not an error, and not the end of anything.
                continue

            for entry_id, fields in entries:
                candle = candle_from_fields(fields)
                yield candle

                # Reached only once the consumer has asked for the next bar, which is the whole
                # design — see the docstring.
                self._client.xack(self._stream, self._group, entry_id)

    def _read(self, cursor: str, *, blocking: bool = True) -> Sequence[tuple[str, dict[str, str]]]:
        """One `XREADGROUP`, flattened to the entries of the single stream we asked about.

        redis-py answers a multi-stream question even when one stream was asked, so the shape
        is `[(stream, [(id, fields), ...])]`. Flattened here rather than at the call site so
        the loop above reads as candles instead of as a wire format.

        `blocking=False` is `backlog()` asking for whatever is there right now. ⚠️ It sends **no
        `BLOCK` clause at all**, which returns at once — not `BLOCK 0`, which in Redis means
        *wait for ever*. The two read alike and are opposites.
        """
        # Blocking only for new bars, and only when the caller wants to wait. Draining the
        # pending list must not wait either way: an empty pending list is the normal case, and
        # blocking on it would stall every clean start-up for the whole timeout.
        block = self._block_ms if (blocking and cursor == _NEW) else None
        response = self._client.xreadgroup(
            groupname=self._group,
            consumername=self._consumer,
            streams={self._stream: cursor},
            block=block,
        )
        if not response:
            return []
        streams = cast("list[tuple[str, list[tuple[str, dict[str, str]]]]]", response)
        return [entry for _stream, entries in streams for entry in entries]

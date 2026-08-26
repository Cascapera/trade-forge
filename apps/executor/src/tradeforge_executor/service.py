"""The loop: one order off the queue, decided, sent or refused, and written down.

Every piece it uses is tested somewhere else without a terminal, a queue or a database. What is
here is the **order the pieces go in**, and two properties that only exist once they are joined:

**Nothing is acknowledged before it is recorded.** The audit write and the `XACK` are in that
order, and it is the one that survives a crash between them: a process that died after acking
and before recording would have sent an order the trail has no memory of. The other way round
costs a duplicate audit row on restart, which is a discrepancy somebody can see.

⚠️ **A duplicate row is not a duplicate order**, and the difference is what makes that trade
acceptable. The redelivered entry is judged again from scratch — and by then the session that
placed it has almost certainly stopped beating, so the second pass refuses it. See
`ledger.session_is_alive`.

**One consumer group for the whole service, shared.** The opposite of the candle stream, which
gives each session its own so every session sees every bar. An order must be sent *once*, by
*one* executor, and a shared group is exactly that primitive. Same Redis feature, opposite
requirement, and the failure of getting it backwards is an order sent twice.
"""

import datetime as dt
import logging
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol, cast

from redis.exceptions import ResponseError
from redis.typing import EncodableT, FieldT, GroupT, KeyT, ResponseT, StreamIdT
from sqlalchemy.orm import Session, sessionmaker

from tradeforge_db.session import session_scope
from tradeforge_executor.ledger import record, session_is_alive
from tradeforge_executor.router import Outcome, Router
from tradeforge_executor.wire import (
    FILLS_STREAM,
    ORDERS_STREAM,
    WireFill,
    WireOrder,
    fill_fields,
    order_from_fields,
)

logger = logging.getLogger(__name__)

__all__ = ["GROUP", "OrderQueue", "Service", "StreamReader"]

GROUP = "executor"
"""One group for the service, not one per session — see the module docstring."""

_PENDING = "0"
_NEW = ">"


class StreamReader(Protocol):
    """The three Redis calls this module makes, spelled in redis-py's own vocabulary.

    ⚠️ `dict` is invariant, so a `streams: dict[str, str]` here would **not** be satisfied by a
    client whose parameter is wider — the protocol would describe a client that does not exist,
    and every double written against it would type-check while the real thing did not. That is
    not a guess; `CandleStream`'s protocol shipped that bug and a test caught it.
    """

    # `id` shadows the builtin, and it is redis-py's parameter name — a Protocol that renamed
    # it would simply not match the client it exists to describe.
    def xgroup_create(
        self,
        name: KeyT,
        groupname: GroupT,
        id: StreamIdT,  # noqa: A002
        mkstream: bool,
    ) -> object: ...

    # ⚠️ `groupname` and `consumername` are plain `str` while the two above take the wider
    # aliases, and the asymmetry is redis-py's, not a slip: `xreadgroup` declares `str` for both.
    # A protocol is satisfied by a *wider* parameter, never a narrower one — copying `GroupT`
    # down here makes the real client fail to match, which is what mypy said when it did.
    def xreadgroup(
        self,
        groupname: str,
        consumername: str,
        streams: dict[KeyT, StreamIdT],
        count: int | None = None,
        block: int | None = None,
    ) -> object: ...

    def xack(self, name: KeyT, groupname: GroupT, *ids: StreamIdT) -> object: ...

    # ⚠️ `dict[FieldT, EncodableT]`, and `ResponseT`. The third time this file has had to be
    # told: a protocol is satisfied by a **wider** parameter and a **narrower** return, and
    # `dict` is invariant on top of that — `dict[str, str]` here is not satisfied by a client
    # whose parameter is wider, however obviously compatible the values look.
    #
    # ⚠️ And **no `maxlen`**, though the real method has one. The real signature puts `id`
    # before it, so declaring `maxlen` third here would let a caller pass it positionally and
    # have it land on `id` — a stream entry with a hand-made id instead of a length cap. The
    # narrow protocol is right for the usual reason too: this module never caps the stream.
    def xadd(self, name: KeyT, fields: dict[FieldT, EncodableT]) -> ResponseT: ...


@dataclass(slots=True)
class OrderQueue:
    """Orders waiting to be sent, oldest first, with the id needed to acknowledge each one."""

    client: StreamReader
    consumer: str
    block_ms: int = 5_000
    stopping: Callable[[], bool] | None = None

    def ensure_group(self) -> bool:
        """Create the consumer group if it is not there. `True` if this call created it.

        ⚠️ `id="0"`, not `"$"`. An executor starting up must inherit whatever is already queued:
        the session that placed those orders is still running, and starting "from now" would
        silently drop every order placed while this service was restarting.
        """
        try:
            self.client.xgroup_create(name=ORDERS_STREAM, groupname=GROUP, id="0", mkstream=True)
        except ResponseError as error:
            if "BUSYGROUP" in str(error).upper():
                return False
            raise
        return True

    def waiting(self) -> Iterator[tuple[str, WireOrder]]:
        """Every order, for ever, oldest first. The pending list first, then new ones.

        ⚠️ **Acknowledgement is the caller's**, deliberately unlike `CandleStream`, which acks
        lazily on the consumer's next request. There, a bar seen twice is a discrepancy somebody
        notices; here, an order sent twice is money. So the ack happens *after* the audit row,
        where the caller can see both.

        ⚠️ **A malformed entry is skipped, not fatal.** It is yielded as nothing and acked by the
        caller — a single bad payload must not wedge the queue behind it for every order after.
        """
        self.ensure_group()
        cursor = _PENDING

        while not self._stopped():
            entries = self._read(cursor)
            if not entries:
                if cursor == _PENDING:
                    cursor = _NEW
                    continue
                continue

            for entry_id, fields in entries:
                try:
                    order = order_from_fields(fields)
                except (KeyError, ValueError):
                    logger.exception("unreadable order on the queue; skipping %s", entry_id)
                    self.ack(entry_id)
                    continue
                yield entry_id, order
                if self._stopped():
                    return

    def ack(self, entry_id: str) -> None:
        self.client.xack(ORDERS_STREAM, GROUP, entry_id)

    def publish_fill(self, fill: WireFill) -> None:
        """One entry on `fills.inbound`. Fan-out: every session reads it with its own group."""
        # The dict is widened at the boundary, where the client's vocabulary starts. Typing
        # `fill_fields` this way instead would push Redis's aliases into the wire module,
        # which has no business knowing what a stream is.
        # ⚠️ A cast, and an honest one: every key and value here *is* a `str`, which is both a
        # `FieldT` and an `EncodableT`. What refuses the assignment is `dict`'s invariance —
        # a type-system fact about the container, not a doubt about the contents.
        fields = cast("dict[FieldT, EncodableT]", fill_fields(fill))
        self.client.xadd(FILLS_STREAM, fields)

    def _stopped(self) -> bool:
        return self.stopping is not None and self.stopping()

    def _read(self, cursor: str) -> Sequence[tuple[str, dict[str, str]]]:
        response = self.client.xreadgroup(
            groupname=GROUP,
            consumername=self.consumer,
            streams={ORDERS_STREAM: cursor},
            block=self.block_ms if cursor == _NEW else None,
        )
        if not response:
            return []
        streams = cast("list[tuple[str, list[tuple[str, dict[str, str]]]]]", response)
        return [entry for _stream, entries in streams for entry in entries]


@dataclass(slots=True)
class Service:
    """The queue, the router and the database, joined for as long as the process lives."""

    queue: OrderQueue
    router: Router
    factory: sessionmaker[Session]
    now: Callable[[], dt.datetime]

    def run(self) -> int:
        """Serve orders until the queue stops yielding. Returns how many it handled."""
        handled = 0
        for entry_id, order in self.queue.waiting():
            self.handle(entry_id, order)
            handled += 1
        return handled

    def handle(self, entry_id: str, order: WireOrder) -> Outcome:
        """One order: decide, act, **record, then acknowledge**.

        ⚠️ The ack is last and it is not a detail. A process that died after acking and before
        recording would have sent an order the audit trail has no memory of — the single failure
        `order_audit` exists to make impossible. The other order costs a duplicate row after a
        restart, and a duplicate row is a discrepancy somebody can see.
        """
        moment = self.now()
        with session_scope(self.factory) as db:
            alive = session_is_alive(db, order.session_id, now=moment)

        outcome = self.router.route_one(order, now=moment, core_is_alive=alive)

        with session_scope(self.factory) as db:
            record(db, order, outcome, now=moment)

        self._publish_fill(order, outcome, at=moment)
        self.queue.ack(entry_id)
        return outcome

    def _publish_fill(self, order: WireOrder, outcome: Outcome, *, at: dt.datetime) -> None:
        """Tell the session what the venue did — but only when the venue actually did it.

        ⚠️ **After the audit row, before the ack**, and both halves matter. After the row,
        because the trail is the record of last resort and must not be behind the thing it
        records. Before the ack, because a fill published for an entry that was then redelivered
        would be published twice — and a session that saw one fill twice would believe it holds
        two positions.

        ⚠️ **Nothing is published for a refusal.** `fills.inbound` says what happened at the
        venue, and a refusal never reached one; a "fill" of zero would be a session waiting for
        a position it will never get told about. The refusal already went to `order_audit`, which
        is where an operator asks why.

        ⚠️ **Nothing is published for a *resting* order either**, which is the same rule and used
        not to be the same code. A limit order accepted by the venue is a live order, not a
        trade: `Placement.filled_volume` is zero until a deal exists (see its docstring), so an
        order waiting in the book falls out here alongside the refusals. Whoever tells the
        session that the limit finally filled, later, is not this method — a deal that happens
        minutes after the entry was acknowledged has nobody in this loop watching for it.
        """
        placement = outcome.placement
        if not outcome.sent or placement is None or placement.filled_volume <= 0:
            return
        if placement.price is None:
            # A deal with a volume and no price is a contradiction, and there is no honest
            # `WireFill` to build from it — `Fill` refuses a price of zero, so publishing one
            # would kill the session with an error about the wrong thing entirely. Refuse loudly
            # instead: `order_audit` already holds the terminal's answer verbatim.
            logger.error(
                "deal %s filled %s of %s with no price; nothing published to %s",
                placement.deal,
                placement.filled_volume,
                order.client_id,
                FILLS_STREAM,
            )
            return
        # `Placement.__post_init__` has already refused a filled volume with no quote, so this
        # is a `Decimal`. Narrowing it again would add a branch no test could enter.
        self.queue.publish_fill(
            WireFill(
                client_id=order.client_id,
                session_id=order.session_id,
                symbol=order.request.symbol,
                at=at,
                price=placement.price,
                volume=placement.filled_volume,
                spread=cast("Decimal", placement.spread),
                ticket=placement.ticket,
            )
        )

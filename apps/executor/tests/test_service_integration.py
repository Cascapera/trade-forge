"""The loop, with a real database and a fake queue.

Every piece is tested somewhere else. What is here is the **order the pieces go in**, and the
two properties that only exist once they are joined: nothing is acknowledged before it is
recorded, and an order from a session that has stopped beating never reaches the venue.
"""

import datetime as dt
import uuid
from collections.abc import Sequence
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from tradeforge_db.live_sessions import STALE_AFTER, beat, open_session
from tradeforge_db.models import Instrument, LiveSession, OrderAudit, OrderAuditStatus, Strategy
from tradeforge_engine.domain import AssetClass, OrderRequest, Side, SignalKind
from tradeforge_executor.gateway import Placement
from tradeforge_executor.router import Router
from tradeforge_executor.safety import Limits
from tradeforge_executor.service import GROUP, OrderQueue, Service
from tradeforge_executor.wire import ORDERS_STREAM, order_fields, order_from_fields

pytestmark = pytest.mark.integration

NOON = dt.datetime(2026, 8, 25, 12, tzinfo=dt.UTC)


class FakeQueue:
    """A stream that hands over a fixed list, and records what was acknowledged."""

    def __init__(self, entries: list[tuple[str, dict[str, str]]]) -> None:
        self._entries = entries
        self.acked: list[str] = []
        self.groups: list[tuple[str, str]] = []

    def xgroup_create(self, name: str, groupname: str, id: str, mkstream: bool) -> object:  # noqa: A002
        # ⚠️ The id is recorded, not swallowed. A double that ignored it would make
        # `id="$"` — which silently drops every order queued during a restart —
        # invisible to every test in this file. It was, until a mutant said so.
        self.groups.append((groupname, id))
        return True

    def xreadgroup(
        self,
        groupname: str,
        consumername: str,
        streams: dict[Any, Any],
        count: int | None = None,
        block: int | None = None,
    ) -> object:
        if not self._entries:
            return []
        handing, self._entries = self._entries, []
        return [(ORDERS_STREAM, handing)]

    def xack(self, name: str, groupname: str, *ids: str) -> object:
        self.acked.extend(ids)
        return len(ids)


class FakeGateway:
    def __init__(self, *, accepted: bool = True, broken: bool = False) -> None:
        self._accepted = accepted
        self._broken = broken
        self.sent: list[str] = []

    def send(self, order: OrderRequest, *, client_id: str) -> Placement:
        if self._broken:
            raise ConnectionError("the terminal went away")
        self.sent.append(client_id)
        return Placement(
            accepted=self._accepted,
            ticket=1 if self._accepted else None,
            filled_volume=Decimal("0.10") if self._accepted else Decimal(0),
            price=Decimal("1.10000") if self._accepted else None,
            retcode=10009 if self._accepted else 10019,
            comment="done" if self._accepted else "no money",
            raw={"retcode": 10009 if self._accepted else 10019},
        )

    def balance(self) -> Decimal:
        return Decimal("10000")

    def open_positions(self) -> int:
        return 0

    def realised_since(self, moment: dt.datetime) -> Decimal:
        return Decimal(0)


class Switch:
    def __init__(self, engaged: bool, *, name: str = "test") -> None:
        self._engaged = engaged
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    def engaged(self) -> bool:
        return self._engaged


def a_live_session(db: Session, *, beating_at: dt.datetime | None = NOON) -> LiveSession:
    instrument = Instrument(
        symbol="EURUSD",
        name="Euro vs US Dollar",
        asset_class=AssetClass.FOREX,
        currency_base="EUR",
        currency_quote="USD",
        tick_size=Decimal("0.00001"),
        tick_value=Decimal("1"),
        contract_size=Decimal("100000"),
        digits=5,
    )
    strategy = Strategy(definition={"schema_version": "1.0", "name": "MA"}, version=1)
    db.add_all([instrument, strategy])
    db.flush()
    row = open_session(
        db,
        strategy_id=strategy.id,
        instrument_id=instrument.id,
        timeframe="H1",
        initial_capital=Decimal("10000"),
        cost_model={"type": "none"},
        engine_version="0.1.0",
        warmup_bars=10,
        at=NOON,
    )
    if beating_at is not None:
        beat(db, row.id, at=beating_at)
    db.commit()
    return row


def an_entry(session_id: str, *, client_id: str = "zone-42") -> tuple[str, dict[str, str]]:
    request = OrderRequest(
        symbol="EURUSD",
        side=Side.LONG,
        intent=SignalKind.ENTRY,
        volume=Decimal("0.10"),
        decided_at=NOON,
        stop_loss=Decimal("1.09500"),
    )
    return ("1-0", order_fields(request, session_id=session_id, client_id=client_id))


def a_service(
    factory: sessionmaker[Session],
    queue: FakeQueue,
    gateway: FakeGateway,
    *,
    switches: Sequence[Switch] = (),
    now: dt.datetime = NOON,
) -> Service:
    return Service(
        queue=OrderQueue(client=queue, consumer="test-1"),  # type: ignore[arg-type]
        router=Router(gateway=gateway, limits=Limits(), switches=switches),
        factory=factory,
        now=lambda: now,
    )


def rows(db: Session) -> list[OrderAudit]:
    return list(db.execute(select(OrderAudit)).scalars().all())


# --------------------------------------------------------------------------------------------
# The order the pieces go in
# --------------------------------------------------------------------------------------------


def test_an_admitted_order_is_sent_recorded_and_then_acknowledged(
    session: Session, session_factory: sessionmaker[Session]
) -> None:
    live = a_live_session(session)
    entry_id, fields = an_entry(str(live.id))
    queue, gateway = FakeQueue([(entry_id, fields)]), FakeGateway()

    a_service(session_factory, queue, gateway).handle(entry_id, order_from_fields(fields))

    assert gateway.sent == ["zone-42"]
    assert [row.status for row in rows(session)] == [OrderAuditStatus.SENT]
    assert queue.acked == [entry_id]


def test_nothing_is_acknowledged_before_it_is_recorded(
    session: Session, session_factory: sessionmaker[Session]
) -> None:
    """⚠️ **The property a crash between the two would break.** A process that died after acking
    and before recording would have sent an order the audit trail has no memory of — the single
    failure `order_audit` exists to make impossible.

    Separated by making the *record* fail: if the ack came first it would already be on the
    queue's books when the write blew up.
    """
    live = a_live_session(session)
    entry_id, fields = an_entry(str(live.id))
    queue, gateway = FakeQueue([(entry_id, fields)]), FakeGateway()
    service = a_service(session_factory, queue, gateway)

    order = order_from_fields(fields)
    # A session id the audit's foreign key will accept but whose row is deleted mid-flight is
    # hard to arrange; refusing the write itself is the same shape and says the same thing.
    object.__setattr__(order, "client_id", "x" * 200)  # longer than the column allows

    with pytest.raises(Exception, match=r"(?i)value too long|character varying"):
        service.handle(entry_id, order)

    assert queue.acked == [], "the entry was acknowledged despite the record failing"


def test_an_order_from_a_session_that_stopped_beating_never_reaches_the_venue(
    session: Session, session_factory: sessionmaker[Session]
) -> None:
    """⚠️ The scenario the whole liveness check exists for: an order sitting in the queue, placed
    by a session that has since died. Sending it would be acting for something that no longer
    exists."""
    live = a_live_session(session, beating_at=NOON)
    entry_id, fields = an_entry(str(live.id))
    queue, gateway = FakeQueue([(entry_id, fields)]), FakeGateway()

    a_service(session_factory, queue, gateway, now=NOON + STALE_AFTER).handle(
        entry_id, order_from_fields(fields)
    )

    assert gateway.sent == [], "an order was sent for a session that had stopped beating"
    (row,) = rows(session)
    assert row.status is OrderAuditStatus.REFUSED
    assert row.reason is not None
    assert "not answering" in row.reason
    assert queue.acked == [entry_id], "a refused order must still be taken off the queue"


def test_a_refused_order_is_still_recorded_and_acknowledged(
    session: Session, session_factory: sessionmaker[Session]
) -> None:
    """⚠️ Acknowledged, not left behind. A refusal that stayed on the queue would be retried for
    ever against the same engaged switch, and the audit trail would grow a row a second."""
    live = a_live_session(session)
    entry_id, fields = an_entry(str(live.id))
    queue, gateway = FakeQueue([(entry_id, fields)]), FakeGateway()

    a_service(session_factory, queue, gateway, switches=[Switch(True, name="the-handle")]).handle(
        entry_id, order_from_fields(fields)
    )

    assert gateway.sent == []
    (row,) = rows(session)
    assert row.status is OrderAuditStatus.REFUSED
    assert "the-handle" in (row.reason or "")
    assert queue.acked == [entry_id]


def test_the_loop_drains_what_is_waiting_and_stops(
    session: Session, session_factory: sessionmaker[Session]
) -> None:
    live = a_live_session(session)
    entries = [
        ("1-0", an_entry(str(live.id), client_id="zone-1")[1]),
        ("2-0", an_entry(str(live.id), client_id="zone-2")[1]),
    ]
    queue, gateway = FakeQueue(entries), FakeGateway()
    stopped = {"yes": False}
    service = a_service(session_factory, queue, gateway)
    service.queue.stopping = lambda: stopped["yes"]

    handled = 0
    for entry_id, order in service.queue.waiting():
        service.handle(entry_id, order)
        handled += 1
        stopped["yes"] = handled >= 2

    assert handled == 2
    assert gateway.sent == ["zone-1", "zone-2"]
    assert len(rows(session)) == 2
    assert queue.acked == ["1-0", "2-0"]


def test_a_stop_is_noticed_between_the_orders_of_one_batch() -> None:
    """⚠️ Inside the batch, not only between reads. One read hands back several orders, and a
    stop noticed only at the top of the loop would send every one of them after somebody asked
    the executor to stop — which is the whole minute an operator was trying to prevent.

    Separated by handing back **one** batch of two: the outer `while` never gets a second turn,
    so only the inner check can end it.
    """
    entries = [
        ("1-0", an_entry("s", client_id="zone-1")[1]),
        ("2-0", an_entry("s", client_id="zone-2")[1]),
    ]
    queue = FakeQueue(entries)
    stop = {"now": False}
    drained = OrderQueue(client=queue, consumer="test-1", stopping=lambda: stop["now"])  # type: ignore[arg-type]

    handed = []
    for entry_id, _order in drained.waiting():
        handed.append(entry_id)
        stop["now"] = True

    assert handed == ["1-0"], f"{len(handed)} orders were handed over after the stop"


def test_the_group_is_created_from_the_beginning_not_from_now() -> None:
    """⚠️ `id="0"`, and the **id** is what this asserts.

    An executor starting up must inherit whatever is already queued: the session that placed
    those orders is still running, and `"$"` would silently drop every order placed while this
    service was restarting — no error, no gap anybody could point at, just orders that never
    happened.

    The first version of this test checked only the group *name*, and a mutant flipping the id
    to `"$"` sailed through it.
    """
    queue = FakeQueue([])
    OrderQueue(client=queue, consumer="test-1").ensure_group()  # type: ignore[arg-type]

    assert queue.groups == [(GROUP, "0")]


def test_an_unreadable_entry_is_acknowledged_rather_than_wedging_the_queue(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """⚠️ A single bad payload must not park itself in front of every order behind it. It is
    logged, acked, and the loop moves on — the dead-letter path this project can afford."""
    queue = FakeQueue([("1-0", {"client_id": "x"}), ("2-0", an_entry(str(uuid.uuid4()))[1])])
    drained = OrderQueue(client=queue, consumer="test-1", stopping=lambda: False)  # type: ignore[arg-type]

    with caplog.at_level("CRITICAL"):
        got = [entry_id for entry_id, _order in _first(drained, 1)]

    assert got == ["2-0"], "the malformed entry was handed over as if it were an order"
    assert "1-0" in queue.acked


def _first(queue: OrderQueue, count: int) -> list[tuple[str, Any]]:
    taken: list[tuple[str, Any]] = []
    for item in queue.waiting():
        taken.append(item)
        if len(taken) >= count:
            break
    return taken

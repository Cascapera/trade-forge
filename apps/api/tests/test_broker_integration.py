"""`MT5Broker` against a real Redis, because the double cannot testify about the protocol.

`FakeStreams` in `test_broker.py` is a description of consumer groups written by the author of
the code it feeds. It proves that module's logic is self-consistent and nothing whatsoever about
whether Redis agrees. The claims that need a server are:

* `venue.outcomes` is **fan-out** — two sessions, two groups, and each sees the same fill. That
  is the opposite of `orders.outbound`, and getting it backwards is silent: a session simply
  never learns its order filled.
* an entry stays on the pending list until it is acked, and comes back to a restarted broker;
* `XGROUP CREATE` on an existing group really does answer `BUSYGROUP`;
* the executor's encoding and this broker's decoding are inverse **over a socket**, not just
  over a dict somebody built in memory. That last one is why this file matters most: the unit
  tests round-trip through `fill_fields`, but they never hand the result to redis-py, which
  encodes, stores and decodes on its own terms. A `Decimal` that survives a dict and dies in a
  socket would pass every test in the other file — and this is a broker whose whole job is
  arithmetic on prices.

Run locally with:

    docker compose up -d redis
    uv run pytest -m integration apps/api/tests/test_broker_integration.py

⚠️ Scoped to this file on purpose. `-m integration` across the whole suite TRUNCATES tables in
whatever database the environment points at; use `POSTGRES_DB=tradeforge_test` for the rest.
"""

import datetime as dt
import uuid
from collections.abc import Iterator
from contextlib import suppress
from decimal import Decimal
from typing import Any, cast

import pytest
from redis import Redis
from redis.exceptions import ResponseError

from tradeforge_api.config import Settings
from tradeforge_api.live.broker import MT5Broker
from tradeforge_engine.domain import (
    AssetClass,
    Candle,
    InstrumentSpec,
    OrderRequest,
    RefusedBy,
    Side,
    SignalKind,
)
from tradeforge_executor.wire import (
    ORDERS_STREAM,
    VENUE_OUTCOMES,
    WireFill,
    WireRefusal,
    fill_fields,
    order_from_fields,
    refusal_fields,
)

pytestmark = pytest.mark.integration

NOON = dt.datetime(2026, 8, 26, 12, tzinfo=dt.UTC)
HOUR = dt.timedelta(hours=1)

EURUSD = InstrumentSpec(
    symbol="EURUSD",
    name="Euro vs US Dollar",
    asset_class=AssetClass.FOREX,
    currency_quote="USD",
    tick_size=Decimal("0.00001"),
    tick_value=Decimal("1"),
    contract_size=Decimal("100000"),
    digits=5,
)


@pytest.fixture
def client() -> Iterator[Redis]:
    """`decode_responses=True`, matching how the rest of the project reads Redis: this broker
    parses `Decimal(field)` from text, and a client handing back bytes would fail at the first
    field rather than at a wrong number."""
    redis = Redis.from_url(Settings().redis_url, decode_responses=True)
    redis.ping()
    yield redis
    redis.close()


@pytest.fixture
def session_id(client: Redis) -> Iterator[str]:
    """A session nobody else is using, and its consumer group destroyed afterwards.

    ⚠️ The **streams** are shared with anything else running against this Redis and are not
    deleted — an executor may legitimately be attached to them. What this file owns is its own
    groups, and leaving those behind would make a second run of the file resume from the first
    run's position, which fails looking exactly like a bug in the broker.
    """
    unique = f"test-{uuid.uuid4()}"
    yield unique
    for group in (f"session-{unique}", f"session-{unique}-b"):
        # The group may never have been created: a test that failed before `ensure_group`
        # leaves nothing to destroy, and that is not a second failure to report.
        with suppress(ResponseError):
            client.xgroup_destroy(VENUE_OUTCOMES, group)


def a_broker(client: Redis, session_id: str, *, group_suffix: str = "") -> MT5Broker:
    broker = MT5Broker(
        client,
        session_id=session_id,
        instrument=EURUSD,
        initial_capital=Decimal("10000"),
    )
    if group_suffix:
        broker._group = f"{broker._group}{group_suffix}"  # a second reader of one stream
    broker.ensure_group()
    return broker


def an_order(**overrides: object) -> OrderRequest:
    values: dict[str, object] = {
        "symbol": "EURUSD",
        "side": Side.LONG,
        "intent": SignalKind.ENTRY,
        "volume": Decimal("1.00"),
        "decided_at": NOON,
        "client_id": "zone-42",
    }
    values.update(overrides)
    return OrderRequest(**values)  # type: ignore[arg-type]


def a_candle(*, time: dt.datetime = NOON + HOUR, close: str = "1.16700") -> Candle:
    body = [Decimal("1.16650"), Decimal(close)]
    return Candle(
        time=time,
        open=Decimal("1.16650"),
        high=max(body) + Decimal("0.00050"),
        low=min(body) - Decimal("0.00050"),
        close=Decimal(close),
        tick_volume=500,
        spread=7,
        real_volume=0,
    )


def publish_fill(client: Redis, session_id: str, *, client_id: str = "zone-42") -> str:
    """Exactly what `Service._publish_fill` writes, through the same encoder."""
    fields = fill_fields(
        WireFill(
            client_id=client_id,
            session_id=session_id,
            symbol="EURUSD",
            at=NOON + HOUR + dt.timedelta(seconds=18),
            price=Decimal("1.16667"),
            volume=Decimal("1.00"),
            spread=Decimal("0.00007"),
            ticket=99,
        )
    )
    # redis-py types the sync client's returns as `Awaitable[Any] | Any`, and `dict` is
    # invariant so a `dict[str, str]` is not the mapping it declares. Both are the client's
    # own typing, not a doubt about the values -- every key and value here is a `str`.
    return str(client.xadd(VENUE_OUTCOMES, cast("dict[Any, Any]", fields)))


def test_the_encoding_survives_a_real_socket(client: Redis, session_id: str) -> None:
    """⚠️ The claim the unit tests cannot make. Written by the executor's encoder, stored by
    redis-py, read back by this broker — and the price has to come out of that as the same
    number, to the last tick, or every P&L the session reports is wrong in the fifth decimal.
    """
    broker = a_broker(client, session_id)
    broker.submit(an_order())
    publish_fill(client, session_id)

    (fill,) = broker.on_bar(a_candle())

    assert fill.price == Decimal("1.16660"), "the ask was not converted to the bid it came from"
    assert str(fill.price) == "1.16660", "the price went through a float in the socket"
    assert fill.costs == Decimal("7")
    assert fill.volume == Decimal("1.00")


def test_the_order_reaches_the_stream_the_executor_reads(client: Redis, session_id: str) -> None:
    """`submit` writes where an executor is actually listening, and the entry decodes back into
    the order that was sent."""
    before = cast("int", client.xlen(ORDERS_STREAM))
    broker = a_broker(client, session_id)
    broker.submit(an_order(volume=Decimal("0.37")))

    entries = cast("list[tuple[str, dict[str, str]]]", client.xrevrange(ORDERS_STREAM, count=1))
    (_entry_id, fields) = entries[0]
    wire = order_from_fields(dict(fields))

    assert cast("int", client.xlen(ORDERS_STREAM)) == before + 1
    assert wire.session_id == session_id
    assert wire.request.volume == Decimal("0.37"), "the size changed on the way out"


def test_two_sessions_each_see_the_same_fill(client: Redis, session_id: str) -> None:
    """⚠️ **Fan-out, and this is the test that says so against a server.**

    `venue.outcomes` is the opposite of `orders.outbound`: an order must be handled by exactly
    one executor, a fill must reach the session that placed it *and* anything else watching. If
    these two groups shared one, each would see roughly half the fills and neither would know.
    """
    first = a_broker(client, session_id)
    second = a_broker(client, session_id, group_suffix="-b")
    first.submit(an_order())
    second.submit(an_order())
    publish_fill(client, session_id)

    assert len(first.on_bar(a_candle())) == 1
    assert len(second.on_bar(a_candle())) == 1, "the second group was handed nothing"


def test_a_fill_read_but_never_acknowledged_comes_back(client: Redis, session_id: str) -> None:
    """The pending list is Redis's, not the broker's. A session that died between reading a fill
    and folding it into its ledger must not resume holding a position it has never heard of."""
    broker = a_broker(client, session_id)
    broker.submit(an_order())
    publish_fill(client, session_id)
    # Read as the broker would, and leave it unacknowledged — which is what dying looks like.
    client.xreadgroup(
        groupname=f"session-{session_id}",
        consumername="session",
        streams={VENUE_OUTCOMES: ">"},
    )

    (fill,) = broker.on_bar(a_candle())

    assert fill.volume == Decimal("1.00"), "the unacknowledged fill was lost"
    assert broker.on_bar(a_candle(time=NOON + 2 * HOUR)) == (), "it came back a second time"


def test_creating_the_group_twice_answers_busygroup(client: Redis, session_id: str) -> None:
    """Read off the error rather than pre-empted with a check, because asking and then writing
    is two round trips with a race between them — and this is the message being matched on."""
    broker = a_broker(client, session_id)

    assert broker.ensure_group() is False, "the second create was reported as having created it"


def test_another_session_s_fill_is_acknowledged_not_left_pending(
    client: Redis, session_id: str
) -> None:
    """The stream is shared, so this group is offered every fill any executor publishes. An
    entry left pending is redelivered on every bar for the life of the session."""
    broker = a_broker(client, session_id)
    publish_fill(client, f"somebody-else-{uuid.uuid4()}", client_id="their-zone")

    assert broker.on_bar(a_candle()) == ()

    pending = cast("dict[str, Any]", client.xpending(VENUE_OUTCOMES, f"session-{session_id}"))
    assert pending["pending"] == 0, "another session's entry was left on the pending list"


def test_a_refusal_written_by_the_executor_is_read_back_as_the_engine_s(
    client: Redis, session_id: str
) -> None:
    """**The seam, over a real socket, with nothing fabricated** (ADR-0024).

    ⚠️ **Written because a unit test on either side proves neither.** The executor's tests assert
    what it publishes; the broker's assert what it reads from a double. Between the two sits
    redis-py and the encoding, and the previous PR in this line was refused by review for exactly
    that gap — one file proving a refusal is born, another proving one is observed, and the
    second fabricating the object. So this one goes through `Service._publish_outcome`'s own
    encoder, a real Redis, and a real `MT5Broker`.

    ⚠️ `retcode` is an `int` on the way in and Redis stores only strings. A round trip that
    handed it back as `"10027"` — or dropped it — would leave a strategy unable to tell "trading
    is off" from "your stop is too close", which are the two ends of whether to try again.
    """
    # `a_broker` already creates the group; `start()` additionally demands a fresh venue
    # snapshot, which is the subject of two other tests and an unrelated reason to fail here.
    broker = a_broker(client, session_id)
    fields = refusal_fields(
        WireRefusal(
            client_id="zone-42",
            session_id=session_id,
            at=NOON + HOUR + dt.timedelta(seconds=18),
            reason="the terminal is not accepting orders",
            by_venue=True,
            retcode=10027,
        )
    )
    client.xadd(VENUE_OUTCOMES, cast("dict[Any, Any]", fields))

    born = broker.on_bar(a_candle())

    assert born == (), "a refusal was folded into the ledger"
    [refusal] = broker.refusals()
    assert refusal.client_id == "zone-42"
    assert refusal.refused_by is RefusedBy.VENUE, "the venue's own refusal came back as ours"
    assert "10027" in refusal.detail, "the retcode did not survive the socket"


def test_a_fill_and_a_refusal_share_one_order_of_arrival(client: Redis, session_id: str) -> None:
    """⚠️ **The reason they share a stream at all**, and the argument the outbound direction
    already makes: *"One stream is one order of arrival. That is the whole argument"*.

    Two streams would let a session read the refusal of one order and the fill of another in
    whichever order the two happened to be polled. Here both are written by the executor's own
    encoders and both come back off one read, each down its own path — the fill into the ledger,
    the refusal into the mailbox — without either being mistaken for the other.
    """
    broker = a_broker(client, session_id)
    broker.submit(an_order(client_id="filled-1"))
    publish_fill(client, session_id, client_id="filled-1")
    client.xadd(
        VENUE_OUTCOMES,
        cast(
            "dict[Any, Any]",
            refusal_fields(
                WireRefusal(
                    client_id="refused-1",
                    session_id=session_id,
                    at=NOON + HOUR + dt.timedelta(seconds=19),
                    reason="no",
                    by_venue=False,
                )
            ),
        ),
    )

    born = broker.on_bar(a_candle())

    assert [fill.order.client_id for fill in born] == ["filled-1"], "the refusal reached the ledger"
    assert [refusal.client_id for refusal in broker.refusals()] == ["refused-1"]

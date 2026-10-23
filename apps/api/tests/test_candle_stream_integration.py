"""`CandleStream` against a real Redis, because the double cannot testify about the protocol.

`tradeforge_api.live.testing.FakeRedisStreams` is a description of consumer groups written by
the author of the code it feeds. It proves that module's logic is self-consistent and nothing
whatsoever about whether Redis agrees. Every claim in `test_candle_stream.py` therefore has a
counterpart here, stated against a server:

* a group created with `$` does not see what was already on the stream, and one created with
  `0` does;
* an entry stays on the pending list until it is acked, and comes back to a restarted consumer;
* `XGROUP CREATE` on an existing group really does answer `BUSYGROUP`;
* the publisher's encoding and this consumer's decoding are inverse over the wire, not just
  over a dict somebody built in memory.

That last one is the reason this file matters most. The unit tests round-trip through
`candle_fields`, but they never hand the result to redis-py — which encodes, stores and decodes
on its own terms. A `Decimal` that survives a dict and dies in a socket would pass every test
in the other file.

Run locally with:

    docker compose up -d redis
    uv run pytest -m integration apps/api/tests/test_candle_stream_integration.py

⚠️ Scoped to this file on purpose. `-m integration` across the whole suite TRUNCATES six
tables in whatever database the environment points at, and there is no separate test database.
"""

import datetime as dt
import uuid
from collections.abc import Iterator
from decimal import Decimal
from itertools import islice
from typing import Any, cast

import pytest
from redis import Redis
from redis.exceptions import ResponseError

from tradeforge_api.config import Settings
from tradeforge_api.live import CandleStream
from tradeforge_api.live.splice import splice
from tradeforge_collector.live import Subscription, stream_name
from tradeforge_collector.publisher import RedisCandlePublisher
from tradeforge_engine.domain import Candle

pytestmark = pytest.mark.integration

START = dt.datetime(2026, 8, 24, 10, tzinfo=dt.UTC)
HOUR = dt.timedelta(hours=1)


def a_candle(index: int, *, close: str = "1.10000", spread: int = 12) -> Candle:
    open_ = Decimal("1.10000")
    body = [open_, Decimal(close)]
    return Candle(
        time=START + index * HOUR,
        open=open_,
        high=max(body) + Decimal("0.00050"),
        low=min(body) - Decimal("0.00050"),
        close=Decimal(close),
        tick_volume=500,
        spread=spread,
        real_volume=0,
    )


@pytest.fixture
def client() -> Iterator[Redis]:
    """`decode_responses=True`, matching how the rest of the project reads Redis — the consumer
    parses `Decimal(field)` from text, and a client handing back bytes would fail at the first
    field rather than at a wrong number."""
    redis = Redis.from_url(Settings().redis_url, decode_responses=True)
    redis.ping()
    yield redis
    redis.close()


@pytest.fixture
def subscription(client: Redis) -> Iterator[Subscription]:
    """A stream nobody else is using, deleted afterwards.

    The symbol carries a uuid because these tests create consumer groups and leave entries
    behind, and a fixed name would make two runs of this file interfere — the second run's `$`
    would resolve past the first run's bars, and the failure would look like a bug in the
    consumer.
    """
    unique = Subscription(symbol=f"TEST{uuid.uuid4().hex[:8].upper()}", timeframe="H1")
    yield unique
    client.delete(stream_name(unique))


def publish(client: Redis, subscription: Subscription, *candles: Candle) -> None:
    publisher = RedisCandlePublisher(client)
    for candle in candles:
        assert publisher.publish(subscription, candle), "the publisher refused its own candle"


def a_stream(client: Redis, subscription: Subscription, **kwargs: Any) -> CandleStream:
    return CandleStream(
        client,
        subscription,
        group=f"session-{uuid.uuid4().hex[:8]}",
        block_ms=2_000,
        **kwargs,
    )


def test_the_encoding_survives_a_real_socket(client: Redis, subscription: Subscription) -> None:
    """Published by the collector's publisher, read back by the session's consumer, over the
    wire. Every field, and the prices as `Decimal` with the quantum they went in with — this is
    the only test in the project that would notice a price silently round-tripped through a
    float on either side."""
    sent = a_candle(0, close="1.10123", spread=37)
    publish(client, subscription, sent)

    got = next(a_stream(client, subscription, start_id="0").candles())

    assert got == sent
    assert str(got.close) == "1.10123", "the quantum changed; equality alone would not see it"
    assert got.spread == 37
    assert got.time == sent.time
    assert got.time.tzinfo is dt.UTC


def test_a_group_started_at_now_does_not_see_the_backlog(
    client: Redis, subscription: Subscription
) -> None:
    """The production default, against the server that implements it. If Redis resolved `$`
    differently from what the double assumes, a paper session would open by replaying a week of
    the past at prices long gone — and would report the result as live."""
    publish(client, subscription, a_candle(0), a_candle(1))

    stream = a_stream(client, subscription, start_id="$")
    assert stream.ensure_group() is True

    publish(client, subscription, a_candle(2, close="1.10500"))

    assert next(stream.candles()) == a_candle(2, close="1.10500")


def test_a_group_started_at_zero_replays_what_is_there(
    client: Redis, subscription: Subscription
) -> None:
    publish(client, subscription, a_candle(0), a_candle(1, close="1.10200"))

    candles = a_stream(client, subscription, start_id="0").candles()

    assert [next(candles) for _ in range(2)] == [a_candle(0), a_candle(1, close="1.10200")]


def test_an_unacknowledged_bar_comes_back_to_a_restarted_session(
    client: Redis, subscription: Subscription
) -> None:
    """The crash-safety claim, against the server. The first session takes bar 0 and dies
    without asking for bar 1, so nothing was ever acked; the second session — same group, same
    consumer name — must be handed bar 0 again before it sees bar 1."""
    publish(client, subscription, a_candle(0), a_candle(1, close="1.10200"))
    group = f"session-{uuid.uuid4().hex[:8]}"

    def session() -> CandleStream:
        return CandleStream(client, subscription, group=group, block_ms=2_000, start_id="0")

    first_life = session().candles()
    assert next(first_life) == a_candle(0)
    first_life.close()

    second_life = session().candles()
    assert next(second_life) == a_candle(0), "Redis did not re-deliver the unconfirmed bar"
    assert next(second_life) == a_candle(1, close="1.10200")


def test_asking_for_the_next_bar_acknowledges_the_last(
    client: Redis, subscription: Subscription
) -> None:
    """Read from the server's own pending-entries count rather than from a counter this code
    keeps, because the claim is about what Redis believes, not about what the module did."""
    publish(client, subscription, a_candle(0), a_candle(1, close="1.10200"))
    group = f"session-{uuid.uuid4().hex[:8]}"
    stream = CandleStream(client, subscription, group=group, block_ms=2_000, start_id="0")

    candles = stream.candles()
    next(candles)
    pending_before = cast("dict[str, Any]", client.xpending(stream_name(subscription), group))

    next(candles)
    pending_after = cast("dict[str, Any]", client.xpending(stream_name(subscription), group))

    assert pending_before["pending"] == 2, "both bars were delivered in one read"
    assert pending_after["pending"] == 1, "asking for the next bar did not confirm the last"
    candles.close()


def test_creating_an_existing_group_answers_busygroup(
    client: Redis, subscription: Subscription
) -> None:
    """The string this module matches on is Redis's, not ours. If a future server worded it
    differently, every session restart would raise instead of resuming — and the unit test
    could not possibly notice, because it raises the message the double was told to raise."""
    group = f"session-{uuid.uuid4().hex[:8]}"
    stream = CandleStream(client, subscription, group=group, block_ms=2_000, start_id="0")

    assert stream.ensure_group() is True
    assert stream.ensure_group() is False

    # And the raw call really does answer BUSYGROUP — `ensure_group` swallowing something else
    # would be indistinguishable from this, since it reports both as `False`.
    with pytest.raises(ResponseError, match="BUSYGROUP"):
        client.xgroup_create(name=stream_name(subscription), groupname=group, id="0", mkstream=True)


def test_two_sessions_on_the_same_symbol_each_see_every_bar(
    client: Redis, subscription: Subscription
) -> None:
    """The mistake the module's docstring warns about, proved rather than asserted.

    A consumer group *divides* a stream between its consumers. Two paper sessions sharing one
    group would each get half the bars — and neither would fail: they would simply trade half a
    market each, and the equity curves would look plausible. Separate groups is what makes that
    impossible, and this is the test that would catch the day somebody "tidied" the group name
    into a constant.
    """
    publish(client, subscription, a_candle(0), a_candle(1, close="1.10200"))

    first = a_stream(client, subscription, start_id="0").candles()
    second = a_stream(client, subscription, start_id="0").candles()

    assert [next(first) for _ in range(2)] == [a_candle(0), a_candle(1, close="1.10200")]
    assert [next(second) for _ in range(2)] == [a_candle(0), a_candle(1, close="1.10200")]

    first.close()
    second.close()


def test_the_backlog_drains_what_is_there_and_ends(
    client: Redis, subscription: Subscription
) -> None:
    """⚠️ The claim the double cannot make. `backlog()` reads with **no `BLOCK` clause**, and
    the whole design rests on two behaviours of the server: a group created at `0` hands back
    the entire backlog rather than a page of it, and a read on a drained stream returns at once
    instead of waiting. Both were measured before the code was written; this is what keeps them
    measured.
    """
    publish(client, subscription, a_candle(0), a_candle(1), a_candle(2))
    stream = a_stream(client, subscription, start_id="0")

    started = dt.datetime.now(dt.UTC)
    drained = list(stream.backlog())
    elapsed = dt.datetime.now(dt.UTC) - started

    assert [candle.time for candle in drained] == [a_candle(i).time for i in range(3)]
    assert elapsed < dt.timedelta(seconds=1), f"the drain waited {elapsed}; it sent a BLOCK"


def test_a_backlog_of_a_thousand_bars_comes_back_in_full(
    client: Redis, subscription: Subscription
) -> None:
    """⚠️ Separating "the server returned everything" from "the server returned a page".

    Nothing in `backlog()` passes `COUNT`, and the loop would keep reading until empty either
    way — so a page limit would not break it. What it *would* break is the reasoning above it:
    a session whose Parquet is a week behind has thousands of bars waiting, and the drain has
    to finish before the cut can be applied.
    """
    bars = [a_candle(index) for index in range(1_000)]
    publish(client, subscription, *bars)

    drained = list(a_stream(client, subscription, start_id="0").backlog())

    assert len(drained) == 1_000
    assert [candle.time for candle in drained] == [candle.time for candle in bars]


def test_a_drained_backlog_leaves_a_real_pending_list_empty(
    client: Redis, subscription: Subscription
) -> None:
    """The ack bookkeeping, against the server's own pending list rather than the double's."""
    publish(client, subscription, a_candle(0), a_candle(1))
    stream = a_stream(client, subscription, start_id="0")
    group = stream._group

    list(stream.backlog())

    summary = cast("dict[str, Any]", client.xpending(stream_name(subscription), group))
    assert summary["pending"] == 0


def test_a_backlog_abandoned_part_way_leaves_a_real_pending_entry(
    client: Redis, subscription: Subscription
) -> None:
    """⚠️ The separating half, and the one that matters if the process dies. Redis must still
    be holding that bar for this consumer — a confirmed bar is one the server will never offer
    again, and the hole it leaves is invisible from every side."""
    publish(client, subscription, a_candle(0), a_candle(1), a_candle(2))
    stream = a_stream(client, subscription, start_id="0")
    group = stream._group

    draining = stream.backlog()
    next(draining)
    next(draining)
    draining.close()

    summary = cast("dict[str, Any]", client.xpending(stream_name(subscription), group))
    assert summary["pending"] == 2, "the held bar, and the two behind it, were miscounted"


def test_the_splice_sorts_a_real_stream_into_warm_up_and_live(
    client: Redis, subscription: Subscription
) -> None:
    """The seam end to end, against a server: history on 'disk' overlapping a real backlog, cut
    by time, with the boundary bar surviving the hand-over.

    Bars 0-2 are the Parquet. The stream carries 1-4, so it overlaps by two and extends by two.
    The session opens once bar 3 has closed, so 0-3 are the warm-up and 4 is its first live bar.
    """
    on_the_wire = [a_candle(1), a_candle(2), a_candle(3), a_candle(4)]
    publish(client, subscription, *on_the_wire)
    on_disk = [a_candle(0), a_candle(1), a_candle(2)]

    candles = splice(
        a_stream(client, subscription, start_id="0"),
        history=lambda: on_disk,
        timeframe=HOUR,
        opened_at=a_candle(3).time + HOUR,
    )

    warmed = [candle.time for candle in candles.warmup()]
    # ⚠️ `islice`, not `list`. `live()` chains into `candles()`, which has no ending by design —
    # it is what `iter_run` consumes for the life of the session. Draining it here would block
    # until somebody killed the test.
    lived = [candle.time for candle in islice(candles.live(), 1)]

    assert warmed == [a_candle(index).time for index in range(4)]
    assert lived == [a_candle(4).time]
    assert candles.warmed == 4
    assert candles.dropped == 2, "the stream's overlap with the disk was not de-duplicated"

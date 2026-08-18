"""The publisher against a real Redis, because its central promise is the server's.

`RedisCandlePublisher` claims that announcing the same bar twice is refused. Nothing in this
package enforces that: the entry id is the candle's opening instant, and it is **Redis** that
rejects an id which is not greater than the stream's last one. A fake publisher agreeing with
that claim would only be agreeing with the test that wrote it.

⚠️ Marked `integration`, so it runs in the CI job that stands a real Redis up and is deselected
everywhere else. It touches no Postgres table — unlike the API's integration suite, which
truncates six of them.
"""

import datetime as dt
import os
from collections.abc import Iterator
from decimal import Decimal
from typing import cast

import pytest
from redis import Redis
from redis.exceptions import ResponseError

from tradeforge_collector.live import Subscription, stream_name
from tradeforge_collector.publisher import RedisCandlePublisher, candle_from_fields, entry_id
from tradeforge_collector.source import Candle

pytestmark = pytest.mark.integration

SUBSCRIPTION = Subscription("TESTEUR", "M5")

# One entry as `xrange` hands it back with `decode_responses=True`: the id, and the flat fields.
StreamEntry = tuple[str, dict[str, str]]


def entries(client: Redis) -> list[StreamEntry]:
    """Everything on the subscription's stream, in order.

    ⚠️ The cast is not laziness. `redis-py` types every command as `Awaitable[Any] | Any`
    because one class serves both the sync and the async client, so the sync return is not
    expressible without narrowing it here — and narrowing it once, in a named helper that says
    what the shape is, beats six `cast`s scattered through the assertions.
    """
    return cast("list[StreamEntry]", client.xrange(stream_name(SUBSCRIPTION)))


def candle(minute: int, *, close: str = "1.10001") -> Candle:
    return Candle(
        time=dt.datetime(2026, 8, 18, 12, minute, tzinfo=dt.UTC),
        open=Decimal("1.10000"),
        high=Decimal("1.10009"),
        low=Decimal("1.09998"),
        close=Decimal(close),
        tick_volume=42,
        spread=3,
        real_volume=1,
    )


@pytest.fixture
def client() -> Iterator[Redis]:
    connection = Redis(
        host=os.environ.get("REDIS_HOST", "localhost"),
        port=int(os.environ.get("REDIS_PORT", "6379")),
        decode_responses=True,
    )
    # ⚠️ Deleted before *and* after. Before, because a previous failed run leaves the stream
    # behind and its last id would make every publish in this file look like a duplicate —
    # which is exactly the assertion under test, so the failure would be indistinguishable from
    # a real one. The symbol is fictional so nothing outside this file can name the same stream.
    connection.delete(stream_name(SUBSCRIPTION))
    try:
        yield connection
    finally:
        connection.delete(stream_name(SUBSCRIPTION))
        connection.close()


def test_a_published_candle_lands_on_its_own_stream(client: Redis) -> None:
    publisher = RedisCandlePublisher(client)

    assert publisher.publish(SUBSCRIPTION, candle(5)) is True

    found = entries(client)
    assert len(found) == 1
    found_id, fields = found[0]
    assert found_id == entry_id(candle(5))
    # And it comes back as the candle that went in — the pair of encoders is what a consumer
    # depends on, and a stream entry is only ever strings.
    assert candle_from_fields(dict(fields)) == candle(5)


def test_the_same_bar_twice_is_refused_by_redis(client: Redis) -> None:
    """The claim this file exists for.

    ⚠️ The refusal comes from the server, not from bookkeeping in this process — which is why
    it survives the collector being killed and restarted, and why two collectors pointed at the
    same symbol cost a rejected write rather than a duplicated bar.
    """
    publisher = RedisCandlePublisher(client)

    assert publisher.publish(SUBSCRIPTION, candle(5)) is True
    assert publisher.publish(SUBSCRIPTION, candle(5)) is False

    assert len(entries(client)) == 1


def test_a_republished_bar_does_not_overwrite_what_was_stored(client: Redis) -> None:
    """⚠️ The interesting half of the duplicate case.

    A second announcement of the same instant carrying *different* prices — a corrected bar, a
    second collector on a different feed — must not silently replace the first. `XADD` refusing
    the id is what guarantees that, and an implementation that fell back to an auto-generated id
    on refusal would store both and leave a consumer to pick.
    """
    publisher = RedisCandlePublisher(client)
    publisher.publish(SUBSCRIPTION, candle(5, close="1.10001"))

    # ⚠️ Still inside the bar's own high and low: `Candle` refuses a body its wicks do not
    # contain, so the "different prices" of this scenario have to be a difference a real feed
    # could produce. The domain type caught the first version of this fixture.
    assert publisher.publish(SUBSCRIPTION, candle(5, close="1.10008")) is False

    found = entries(client)
    assert len(found) == 1
    assert candle_from_fields(dict(found[0][1])).close == Decimal("1.10001")


def test_later_bars_keep_arriving_after_a_refusal(client: Redis) -> None:
    # ⚠️ A duplicate must not poison the stream. If the refusal left the publisher or the stream
    # in a state where the next bar could not be written, a single repeated poll would stop the
    # feed for good — and the symptom would appear minutes later, far from the cause.
    publisher = RedisCandlePublisher(client)
    publisher.publish(SUBSCRIPTION, candle(5))
    publisher.publish(SUBSCRIPTION, candle(5))

    assert publisher.publish(SUBSCRIPTION, candle(10)) is True

    ids = [found_id for found_id, _ in entries(client)]
    assert ids == [entry_id(candle(5)), entry_id(candle(10))]


def test_an_error_that_is_not_a_duplicate_is_raised(client: Redis) -> None:
    """⚠️ The guard reads the message, so it has to be shown a different message.

    A key that is not a stream is the reachable case: `XADD` against a plain string fails with
    WRONGTYPE, and swallowing that as "already published" would make a misconfigured collector
    look like a quiet one.
    """
    name = stream_name(SUBSCRIPTION)
    client.delete(name)
    client.set(name, "not a stream")

    with pytest.raises(ResponseError):
        RedisCandlePublisher(client).publish(SUBSCRIPTION, candle(5))

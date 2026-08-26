"""The venue snapshot: what it publishes, and what it refuses to publish.

The thread itself is not exercised here — `publish_once` is, which is the whole of what the
thread does. What a test can say about `threading` is that it started; what matters is what gets
written, and when nothing does.
"""

import datetime as dt
from decimal import Decimal

import redis

from tradeforge_engine.domain import Side
from tradeforge_executor.gateway import MT5Gateway
from tradeforge_executor.snapshot import (
    PUBLISH_EVERY,
    PositionReader,
    StateWriter,
    VenueSnapshot,
)
from tradeforge_executor.wire import (
    VENUE_STATE,
    VENUE_STATE_FRESH_FOR,
    HeldPosition,
    venue_state_from,
)

NOW = dt.datetime(2026, 8, 26, 20, tzinfo=dt.UTC)


class FakeRedis:
    def __init__(self, *, writable: bool = True) -> None:
        self.keys: dict[str, tuple[str, int | None]] = {}
        self._writable = writable

    def set(self, name: object, value: object, ex: int | None = None) -> object:
        if not self._writable:
            raise ConnectionError("redis went away")
        self.keys[str(name)] = (str(value), ex)
        return True


class FakeGateway:
    def __init__(self, *positions: HeldPosition, readable: bool = True) -> None:
        self._positions = positions
        self._readable = readable
        self.asked = 0

    def holdings(self) -> tuple[HeldPosition, ...]:
        self.asked += 1
        if not self._readable:
            raise ConnectionError("the terminal went away")
        return self._positions

    # ⚠️ The rest of `OrderGateway` is **absent, not stubbed**. `VenueSnapshot` is typed against
    # the protocol but calls exactly one of its methods, and a double that answered the other six
    # would be describing a gateway this module has never met. A method nobody calls cannot be
    # modelled wrongly — the same argument `FakeRedisStreams` makes about everything Redis does.


def a_position() -> HeldPosition:
    return HeldPosition(
        ticket=47_096_513,
        symbol="EURUSD",
        side=Side.LONG,
        volume=Decimal("0.01"),
        price_open=Decimal("1.16524"),
        stop_loss=Decimal("1.16014"),
    )


def a_snapshot(gateway: FakeGateway, client: FakeRedis) -> VenueSnapshot:
    return VenueSnapshot(client, gateway, now=lambda: NOW)


def test_what_the_venue_holds_survives_the_round_trip() -> None:
    client = FakeRedis()
    assert a_snapshot(FakeGateway(a_position()), client).publish_once()

    (raw, _ttl) = client.keys[VENUE_STATE]
    state = venue_state_from(raw)

    assert state.at == NOW
    (position,) = state.positions
    assert position.ticket == 47_096_513
    assert str(position.price_open) == "1.16524", "the price went through a float"
    assert position.stop_loss == Decimal("1.16014")


def test_a_flat_account_publishes_an_empty_snapshot_not_nothing() -> None:
    """⚠️ **"I asked and it holds nothing" is an answer, and it has to be written down.** A reader
    that found no key would refuse to start — correctly, because absent means "I do not know" —
    so a flat account that published nothing would be indistinguishable from a dead executor."""
    client = FakeRedis()
    assert a_snapshot(FakeGateway(), client).publish_once()

    (raw, _ttl) = client.keys[VENUE_STATE]
    assert venue_state_from(raw).positions == ()


def test_a_terminal_that_cannot_be_read_writes_nothing_at_all() -> None:
    """⚠️ The one that matters most. Writing an empty document on failure would turn "I could not
    ask" into "the account holds nothing" — silently, at the exact moment the answer decides
    whether a session may start. The old key is left to go stale instead, and stale reads as "I
    do not know"."""
    client = FakeRedis()
    gateway = FakeGateway(readable=False)

    assert a_snapshot(gateway, client).publish_once() is False
    assert gateway.asked == 1, "it did not even try"
    assert client.keys == {}, "a failure to read was published as an empty account"


def test_a_redis_that_will_not_take_the_write_is_not_a_crash() -> None:
    """The snapshot thread must not take the executor down with it: the order loop is the thing
    that matters, and it can keep working while nobody can read the snapshot."""
    assert a_snapshot(FakeGateway(a_position()), FakeRedis(writable=False)).publish_once() is False


def test_the_key_expires_before_a_reader_could_trust_it() -> None:
    """Belt to the timestamp's braces: the stamp lets a reader say *how* stale, which the refusal
    message needs; the TTL means a dead executor's key eventually disappears rather than
    lingering as a plausible answer about an account nobody is watching."""
    client = FakeRedis()
    a_snapshot(FakeGateway(), client).publish_once()

    (_raw, ttl) = client.keys[VENUE_STATE]
    assert ttl is not None
    assert ttl > VENUE_STATE_FRESH_FOR.total_seconds(), "the key dies before it is even stale"


def test_the_publishing_interval_leaves_room_for_one_missed_beat() -> None:
    """⚠️ Not decoration: if `PUBLISH_EVERY` were equal to the freshness window, a single slow
    beat would read as an outage and refuse a session for no reason."""
    assert PUBLISH_EVERY * 2 < VENUE_STATE_FRESH_FOR
    assert PUBLISH_EVERY * 3 >= VENUE_STATE_FRESH_FOR


def test_the_real_redis_client_satisfies_the_writer_protocol() -> None:
    """Proved by assignment, because that is the only thing mypy checks — `isinstance` on a
    protocol compares names, never signatures."""
    writer: StateWriter = redis.Redis()
    assert writer is not None


def test_the_real_gateway_satisfies_the_position_reader() -> None:
    """The other assignment. `VenueSnapshot` asks the venue exactly one question, so it asks for
    exactly one method — and `MT5Gateway` answers it without knowing this module exists."""
    reader: PositionReader = MT5Gateway()
    assert reader is not None

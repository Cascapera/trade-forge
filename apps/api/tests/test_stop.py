"""Asking one live session to stop — the flag, and the predicate that reads it.

The dangerous mutants here are not typos. Two of them are *plausible implementations somebody
would defend*: copying the kill switch's fail-closed policy, and forgetting that the key is per
session. Each has a test named after what it would break.
"""

import datetime as dt
import uuid
from collections.abc import Mapping
from typing import Any

import pytest
import redis
from redis.exceptions import ConnectionError as RedisConnectionError

from tradeforge_api.live.stop import (
    StopStore,
    request_stop,
    requested_at,
    stop_key,
    stop_predicate,
    stop_requested,
)

NOW = dt.datetime(2026, 9, 1, 12, 30, tzinfo=dt.UTC)


class _FakeRedis:
    """Keys in a dict, answering the way a client built with `decode_responses=True` does."""

    def __init__(self, initial: Mapping[str, str] | None = None, *, broken: bool = False) -> None:
        self.keys: dict[str, str] = dict(initial or {})
        self.broken = broken
        self.reads = 0

    def _check(self) -> None:
        if self.broken:
            raise RedisConnectionError("Error 111 connecting to localhost:6379.")

    def set(self, name: Any, value: Any) -> bool:
        self._check()
        self.keys[str(name)] = str(value)
        return True

    def exists(self, *names: Any) -> int:
        self.reads += 1
        self._check()
        return sum(1 for name in names if name in self.keys)

    def get(self, name: Any) -> str | None:
        self._check()
        return self.keys.get(name)


def _id(last: int) -> uuid.UUID:
    return uuid.UUID(f"00000000-0000-0000-0000-00000000000{last}")


# --------------------------------------------------------------------------- #
# The key                                                                      #
# --------------------------------------------------------------------------- #


def test_a_stop_is_addressed_to_one_session_and_not_to_the_others() -> None:
    """⚠️ **The mutant this exists for is a single global key**, and it is not a typo — it is the
    shape the kill switch next door genuinely has, one key for the whole machine.

    Here it would be a catastrophe of exactly the kind that looks like a success: press stop on
    one strategy and every session on the box winds down, each of them reporting a clean,
    deliberate stop. Nothing raises, nothing is logged as wrong, and the panel shows three
    sessions that "somebody stopped".
    """
    store = _FakeRedis()

    request_stop(store, _id(1), now=NOW)

    assert stop_requested(store, _id(1)) is True
    assert stop_requested(store, _id(2)) is False


def test_the_key_names_the_session() -> None:
    """Spelled out once, because the panel and an operator with `redis-cli` both need to find it."""
    assert stop_key(_id(1)) == "live-session:00000000-0000-0000-0000-000000000001:stop"


# --------------------------------------------------------------------------- #
# Requesting                                                                   #
# --------------------------------------------------------------------------- #


def test_requesting_a_stop_records_when_it_was_asked() -> None:
    store = _FakeRedis()

    request_stop(store, _id(1), now=NOW)

    assert store.keys[stop_key(_id(1))] == NOW.isoformat()
    assert requested_at(store, _id(1)) == NOW


def test_asking_twice_is_not_an_error() -> None:
    """A screen that lost the response and retried must not turn a stop into a failure."""
    store = _FakeRedis()

    request_stop(store, _id(1), now=NOW)
    request_stop(store, _id(1), now=NOW + dt.timedelta(minutes=1))

    assert stop_requested(store, _id(1)) is True


def test_the_request_has_no_expiry() -> None:
    """⚠️ **The first draft set an hour's TTL and it is wrong in the direction that matters.**

    A machine asleep past the expiry wakes up and goes on trading a session somebody ended —
    silently, and towards trading rather than away from it. This asserts the call shape rather
    than a behaviour, because "no TTL" is an absence: `set` is called with a name and a value and
    nothing else, so there is no expiry to get wrong.
    """
    calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    class _Recording(_FakeRedis):
        def set(self, *args: Any, **kwargs: Any) -> bool:
            calls.append((args, kwargs))
            return super().set(*args, **kwargs)

    request_stop(_Recording(), _id(1), now=NOW)

    assert len(calls) == 1
    args, kwargs = calls[0]
    assert len(args) == 2, "a third positional argument would be an expiry"
    assert kwargs == {}


# --------------------------------------------------------------------------- #
# Reading, and the failure policy                                              #
# --------------------------------------------------------------------------- #


def test_a_session_nobody_asked_about_is_not_stopping() -> None:
    assert stop_requested(_FakeRedis(), _id(1)) is False
    assert requested_at(_FakeRedis(), _id(1)) is None


def test_an_unreachable_redis_means_no_request_and_the_session_keeps_running() -> None:
    """⚠️⚠️ **Fails OPEN, and the mutant that fails closed is the one somebody would write.**

    `RedisFlag` — twenty lines away in the executor — answers `True` on an unreadable Redis, and
    copying that here for consistency passes every other test in this file. It would be wrong:
    that switch decides whether to take on *new risk*, where refusing costs a missed trade. This
    decides whether to abandon a position already open, with no trailing stop and nobody
    watching, because a network blinked.

    The safe direction is already covered by the switch that fails closed. This one is not the
    safety mechanism, and pretending it is makes a transient fault end a live strategy.
    """
    assert stop_requested(_FakeRedis(broken=True), _id(1)) is False


def test_an_unreadable_stamp_is_dropped_rather_than_raised() -> None:
    """It is decoration for a screen. A hand-written key must not take out the panel."""
    store = _FakeRedis({stop_key(_id(1)): "logo depois do almoco"})

    assert requested_at(store, _id(1)) is None
    assert stop_requested(store, _id(1)) is True, "the request still stands; only the time is lost"


# --------------------------------------------------------------------------- #
# The predicate                                                                #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("signalled", "asked", "expected"),
    [
        (False, False, False),
        (False, True, True),
        (True, False, True),
        (True, True, True),
    ],
)
def test_either_half_stops_the_session(signalled: bool, asked: bool, expected: bool) -> None:
    """⚠️ **All four rows, because `and` passes two of them.**

    A predicate that required *both* would answer correctly for `(False, False)` and
    `(True, True)` — and a session would then ignore Ctrl-C unless somebody had also pressed the
    button, which is the failure this whole PR exists to remove rather than add.
    """
    store = _FakeRedis()
    if asked:
        request_stop(store, _id(1), now=NOW)

    should_stop = stop_predicate(store, _id(1), signalled=lambda: signalled)

    assert should_stop() is expected


def test_a_signalled_process_never_asks_redis() -> None:
    """Not correctness — cost, and it is consulted once per blocked read for a session's whole life.

    ⚠️ It is also the one property a `stop_requested(...) or signalled()` mutant breaks while
    answering every question above identically.
    """
    store = _FakeRedis()

    assert stop_predicate(store, _id(1), signalled=lambda: True)() is True

    assert store.reads == 0


def test_the_predicate_is_read_every_time_it_is_called() -> None:
    """A session asks repeatedly; the answer must be able to change from `False` to `True`.

    ⚠️ The mutant is an eager evaluation — computing the verdict once when the predicate is built
    — and it is easy to write and impossible to notice: every session would simply never honour a
    request that arrived after it started, which is *every* request.
    """
    store = _FakeRedis()
    should_stop = stop_predicate(store, _id(1), signalled=lambda: False)
    assert should_stop() is False

    request_stop(store, _id(1), now=NOW)

    assert should_stop() is True


def test_the_protocol_describes_the_real_client() -> None:
    """Proved by assignment, because a protocol is only checked where something is assigned."""
    store: StopStore = redis.Redis()
    assert store is not None

"""`/executor/kill-switch` — the only route here whose failure costs money.

⚠️ **The fake below is the weak point of this file and it is used deliberately anyway.** A
double written by the author of the code it feeds agrees with the wrong implementation as
readily as the right one; this project has watched twenty-two mutants survive against one. What
carries the weight instead is the pair of tests that run the executor's **real** `RedisFlag`
against whatever this API wrote — the cross-process contract, checked without a container — plus
`test_kill_switch_integration`, which repeats the same claim against a real Redis.
"""

import datetime as dt
from collections.abc import Iterator, Mapping
from typing import Any

import pytest
import redis
from fastapi.testclient import TestClient
from redis.exceptions import ConnectionError as RedisConnectionError

from tradeforge_api.config import Settings
from tradeforge_api.kill_switch import ENGAGED_AT_KEY, KillSwitch, SwitchStore
from tradeforge_api.main import create_app
from tradeforge_executor.kill_switch import ENGAGED_VALUE, SWITCH_KEY, RedisFlag

ROUTE = "/executor/kill-switch"


class _FakeQueue:
    async def enqueue_job(self, *args: object, **options: object) -> None:
        return None


class _FakeRedis:
    """Keys in a dict, answering the way a client built with `decode_responses=True` does.

    ⚠️ **`get` returns `str`, not `bytes`**, because that is what the lifespan's client returns —
    a fake handing back bytes would exercise a branch production never takes and leave the one it
    does take untested.
    """

    def __init__(self, initial: Mapping[str, str] | None = None, *, broken: bool = False) -> None:
        self.keys: dict[str, str] = dict(initial or {})
        self.broken = broken

    def _check(self) -> None:
        if self.broken:
            # The real exception type, not a sentinel: the router catches `RedisError`, and a
            # fake raising anything else would prove the handler catches something nothing throws.
            raise RedisConnectionError("Error 111 connecting to localhost:6379.")

    def exists(self, *names: Any) -> int:
        self._check()
        return sum(1 for name in names if name in self.keys)

    def get(self, name: Any) -> str | None:
        self._check()
        return self.keys.get(name)

    def mset(self, mapping: Mapping[Any, Any]) -> bool:
        self._check()
        self.keys.update({str(key): str(value) for key, value in mapping.items()})
        return True


def _client(store: _FakeRedis) -> Iterator[TestClient]:
    app = create_app(
        settings=Settings(postgres_password="unused-in-unit-tests"),
        arq_pool=_FakeQueue(),
        kill_switch=KillSwitch(store),
    )
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def store() -> _FakeRedis:
    return _FakeRedis()


@pytest.fixture
def client(store: _FakeRedis) -> Iterator[TestClient]:
    yield from _client(store)


# --------------------------------------------------------------------------- #
# The contract that crosses the process boundary                               #
# --------------------------------------------------------------------------- #


def test_what_the_api_writes_is_what_the_executor_reads(
    client: TestClient, store: _FakeRedis
) -> None:
    """The whole point of the PR, and the one assertion no fake can fudge.

    `RedisFlag` is the executor's own class, imported and run here over the same store the route
    just wrote to. If the key ever drifts — a rename on either side, a typo in a literal — this
    fails, and it fails without a container. The alternative was asserting the string
    `"executor:kill-switch"` in two files, which is the copy this design exists to avoid.
    """
    flag = RedisFlag(store)
    assert flag.engaged() is False

    assert client.post(ROUTE).status_code == 200

    assert flag.engaged() is True


def test_the_layer_reported_is_the_name_the_audit_trail_uses(
    client: TestClient, store: _FakeRedis
) -> None:
    """`layer` is `RedisFlag.name`, so the screen and `order_audit` say the same words.

    The executor writes *"kill switch engaged (redis:executor:kill-switch)"* as the refusal
    reason. Somebody reading a refused order and somebody looking at the button have to be able
    to tell they are looking at one fact.
    """
    body = client.post(ROUTE).json()
    assert body["layer"] == RedisFlag(store).name


def test_the_value_written_is_the_one_the_executor_documents(
    client: TestClient, store: _FakeRedis
) -> None:
    """Presence is what engages it, but the writers agree on a value, and this is a writer.

    ⚠️ Asserting the value matters *because* nothing reads it: a drifting write would never be
    caught by behaviour, only by somebody running `redis-cli GET` during an incident and finding
    a string that means nothing to them.
    """
    client.post(ROUTE)
    assert store.keys[SWITCH_KEY] == ENGAGED_VALUE


# --------------------------------------------------------------------------- #
# Engaging                                                                     #
# --------------------------------------------------------------------------- #


def test_engaging_stamps_when_it_happened(client: TestClient, store: _FakeRedis) -> None:
    before = dt.datetime.now(dt.UTC)
    body = client.post(ROUTE).json()

    assert body["engaged"] is True
    stamped = dt.datetime.fromisoformat(body["engaged_at"])
    assert before <= stamped <= dt.datetime.now(dt.UTC)
    assert store.keys[ENGAGED_AT_KEY] == stamped.isoformat()


def test_pressing_twice_keeps_the_first_time(client: TestClient, store: _FakeRedis) -> None:
    """Idempotent, and the second press must not rewrite the audit.

    ⚠️ **The test that separates this from the obvious implementation.** An unconditional `mset`
    passes every other case in this file — it engages, it reports engaged, it is safe to repeat —
    and quietly moves the recorded time forward every time somebody refreshes the screen. With no
    authentication on this route, that timestamp is the only fact recorded about the press.
    """
    first = client.post(ROUTE).json()["engaged_at"]
    stored = store.keys[ENGAGED_AT_KEY]

    second = client.post(ROUTE).json()["engaged_at"]

    assert second == first
    assert store.keys[ENGAGED_AT_KEY] == stored


def test_the_time_reported_is_the_time_stored(client: TestClient, store: _FakeRedis) -> None:
    """The body and the key describe the same instant, compared as instants.

    ⚠️ **Compared parsed, not as text, and the first draft of the test above got this wrong.**
    Python writes `+00:00` and Pydantic serialises the same moment as `Z`, so a string comparison
    across the two fails while nothing is broken. The opposite mistake is the one this project
    has a rule about — `Decimal` compares numerically, so quantisation loss hides behind `==` and
    only the wire text catches it. Which comparison is the honest one depends on whether the
    *rendering* is the contract. Here it is not: the stamp is decoration for a screen, and the
    instant is the fact.
    """
    body = client.post(ROUTE).json()

    assert dt.datetime.fromisoformat(body["engaged_at"]) == dt.datetime.fromisoformat(
        store.keys[ENGAGED_AT_KEY]
    )


def test_engaging_writes_both_keys_in_one_call() -> None:
    """No window in which the switch is on with no stamp, or stamped without being on.

    Counting the calls rather than the keys, because that is the property: two `set`s would
    satisfy every assertion above and still leave a gap a crash can land in.
    """
    calls: list[Mapping[Any, Any]] = []

    class _Counting(_FakeRedis):
        def mset(self, mapping: Mapping[Any, Any]) -> bool:
            calls.append(mapping)
            return super().mset(mapping)

    store = _Counting()
    for test_client in _client(store):
        test_client.post(ROUTE)

    assert len(calls) == 1
    assert set(calls[0]) == {SWITCH_KEY, ENGAGED_AT_KEY}


# --------------------------------------------------------------------------- #
# Reading                                                                      #
# --------------------------------------------------------------------------- #


def test_a_switch_nobody_engaged_reads_as_released(client: TestClient, store: _FakeRedis) -> None:
    body = client.get(ROUTE).json()
    assert body == {"engaged": False, "engaged_at": None, "layer": RedisFlag(store).name}


def test_a_switch_engaged_by_hand_reports_engaged_with_no_time() -> None:
    """`redis-cli SET executor:kill-switch engaged` leaves no stamp, and that is not an error.

    ⚠️ The answer is `null`, never "now" and never the epoch. A screen shown an invented time
    would be telling an operator something nobody established — the same rule the instruments
    table keeps about a missing spread.
    """
    store = _FakeRedis({SWITCH_KEY: ENGAGED_VALUE})
    for test_client in _client(store):
        body = test_client.get(ROUTE).json()

    assert body["engaged"] is True
    assert body["engaged_at"] is None


def test_an_unreadable_stamp_is_dropped_and_never_raises() -> None:
    """The one parse in the module decides a line of text, so it may not decide an outage.

    A hand-written key holding rubbish must not take down the endpoint that exists for when
    things are already going wrong.
    """
    store = _FakeRedis({SWITCH_KEY: ENGAGED_VALUE, ENGAGED_AT_KEY: "ontem de tarde"})
    for test_client in _client(store):
        response = test_client.get(ROUTE)

    assert response.status_code == 200
    assert response.json() == {"engaged": True, "engaged_at": None, "layer": RedisFlag(store).name}


def test_a_stamp_left_behind_by_a_release_is_not_reported() -> None:
    """`DEL executor:kill-switch` alone is the documented release, so the stamp outlives it.

    Reporting it would show a released switch with an engagement time beside it — which reads,
    on a screen, as "engaged".
    """
    store = _FakeRedis({ENGAGED_AT_KEY: "2026-09-01T12:00:00+00:00"})
    for test_client in _client(store):
        body = test_client.get(ROUTE).json()

    assert body == {"engaged": False, "engaged_at": None, "layer": RedisFlag(store).name}


# --------------------------------------------------------------------------- #
# When Redis is not there                                                      #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("method", ["get", "post"])
def test_an_unreachable_redis_is_503_and_never_a_verdict(method: str) -> None:
    """⚠️ **Not `engaged: false`.** The executor fails *closed* on an unreadable Redis — it is at
    that moment refusing every order it is handed. An API that answered `false` would be telling
    an operator the machine is live while it is halted, which is the worst of the three possible
    answers. "I could not ask" is the honest one, and 503 is how HTTP says it.
    """
    store = _FakeRedis(broken=True)
    for test_client in _client(store):
        response = getattr(test_client, method)(ROUTE)

    assert response.status_code == 503
    assert "kill switch" in response.json()["detail"]


def test_the_503_points_at_the_layer_that_still_works() -> None:
    """An operator reading this has to be told what to do next, not just that we failed.

    The file layer lives on the executor's own disk and does not depend on Redis, which is the
    whole reason it exists — so the failure that makes this endpoint useless is exactly the
    failure that layer was designed for.
    """
    store = _FakeRedis(broken=True)
    for test_client in _client(store):
        detail = test_client.post(ROUTE).json()["detail"]

    assert "file layer" in detail


# --------------------------------------------------------------------------- #
# The absence that is a decision                                               #
# --------------------------------------------------------------------------- #


def test_there_is_no_way_to_release_over_http(client: TestClient) -> None:
    """⚠️ **A decision, pinned as a test, because the next person will want to add it.**

    Guilherme's call on 31/08: the button engages, a shell releases. `EndpointFlag` makes the
    same argument about its own layer — an endpoint that can un-kill is an endpoint a retry loop
    can un-kill. This asserts 405 rather than 404: the path exists, the verb does not, which is
    the difference between "not built yet" and "refused on purpose".
    """
    assert client.delete(ROUTE).status_code == 405
    assert client.put(ROUTE).status_code == 405


def test_the_documented_release_actually_releases(client: TestClient, store: _FakeRedis) -> None:
    """The shell command in the module docstring, executed as the docstring spells it.

    ⚠️ A `DEL` of one key, not two — and this proves the sibling stamp does not keep the switch
    alive. Documentation that nothing runs is documentation that stops being true; this project
    has a rule about that, learned from a docstring promising a one-minute shutdown nothing
    implemented.
    """
    client.post(ROUTE)
    assert RedisFlag(store).engaged() is True

    del store.keys[SWITCH_KEY]  # redis-cli DEL executor:kill-switch

    assert RedisFlag(store).engaged() is False
    assert client.get(ROUTE).json()["engaged"] is False


def test_the_protocol_describes_the_real_client() -> None:
    """⚠️ Proved by assignment, because a protocol is only checked where something is assigned.

    This line is a no-op at runtime and the entire point of it is that **mypy** reads it. A
    protocol nothing concrete is ever assigned to describes an imaginary client: `FlagStore` next
    door was wrong in two directions at once and type-checked perfectly until `process.py` handed
    it a real `Redis`.
    """
    store: SwitchStore = redis.Redis()
    assert store is not None

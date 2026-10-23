"""The kill-switch route against a real Redis, with the executor's real reader on the other end.

⚠️ **Why this exists when `test_kill_switch` already passes.** Everything there runs against a
dict written by the author of the code it feeds, and this project has measured what that is
worth: a fake that agreed with the right implementation *and* the wrong one survived twenty-two
mutants, and one minute against the real thing found the divergence. What is unproven until here
is that `redis-py` behaves the way `SwitchStore` assumes — that `mset` writes what `exists` sees,
and that a client built with `decode_responses=True` hands the stamp back as text.

⚠️ **On database 15, and that is the only thing here that is not production-shaped.** The key is
`SWITCH_KEY` itself — the real name, not a test one, because the name is the contract. What is
moved aside is the *database*: everything in this system (`redis_url`, the executor, arq) is
hard-coded to `/0`, so a test writing the real key on `/0` would engage the real executor's real
kill switch and leave it engaged. The next live session would refuse every entry with nothing
raised anywhere — the same shape as the integration run that truncated `trades` on 28/08.

Run with:  docker compose up -d  &&  POSTGRES_DB=tradeforge_test uv run pytest -m integration
"""

import datetime as dt
from collections.abc import Iterator
from typing import Any

import pytest
import redis
from fastapi.testclient import TestClient

from tradeforge_api.config import Settings
from tradeforge_api.kill_switch import ENGAGED_AT_KEY, KillSwitch, SwitchStore
from tradeforge_api.main import create_app
from tradeforge_executor.kill_switch import ENGAGED_VALUE, SWITCH_KEY, RedisFlag

pytestmark = pytest.mark.integration

ROUTE = "/executor/kill-switch"

SANDBOX_DB = 15
"""A logical database nothing in this system is configured to use. See the module docstring."""


class _FakeQueue:
    async def enqueue_job(self, *args: Any, **options: Any) -> None:
        return None


@pytest.fixture
def store(settings: Settings) -> Iterator[redis.Redis]:
    """A real client on the sandbox database, emptied of these two keys before and after.

    Cleaned on the way *in* as well as out, so a run killed halfway through does not leave the
    next one starting from an engaged switch — the failure would look like the route reporting
    state it never wrote.
    """
    client: redis.Redis = redis.Redis(
        host=settings.redis_host,
        port=settings.redis_port,
        db=SANDBOX_DB,
        decode_responses=True,
    )
    client.delete(SWITCH_KEY, ENGAGED_AT_KEY)
    try:
        yield client
    finally:
        client.delete(SWITCH_KEY, ENGAGED_AT_KEY)
        client.close()


@pytest.fixture
def client(settings: Settings, store: redis.Redis) -> Iterator[TestClient]:
    app = create_app(
        settings=settings,
        session_factory=lambda: None,  # type: ignore[arg-type, return-value]  # never opened
        arq_pool=_FakeQueue(),
        kill_switch=KillSwitch(store),
    )
    with TestClient(app) as test_client:
        yield test_client


def test_a_real_redis_round_trips_the_switch_and_its_stamp(
    client: TestClient, store: redis.Redis
) -> None:
    """One `mset`, read back by `exists` and by `get`, through the real driver.

    ⚠️ The stamp is asserted as `str`. `decode_responses=True` is what makes that true, and it is
    set in exactly one place — `main._lifespan`. A client built without it hands back `bytes`,
    `datetime.fromisoformat` raises `TypeError`, and the route 500s on the *engaged* path only —
    the path nobody exercises until the day it matters.
    """
    body = client.post(ROUTE).json()
    stamp = store.get(ENGAGED_AT_KEY)

    assert body["engaged"] is True
    assert store.get(SWITCH_KEY) == ENGAGED_VALUE
    assert isinstance(stamp, str)
    # Parsed, not compared as text: Python writes `+00:00` and Pydantic serialises the same
    # instant as `Z`. The rendering is not the contract here — the instant is.
    assert dt.datetime.fromisoformat(stamp) == dt.datetime.fromisoformat(body["engaged_at"])


def test_the_executors_own_reader_sees_the_switch_this_api_engaged(
    client: TestClient, store: redis.Redis
) -> None:
    """The acceptance criterion of PR-304-C1, in three lines and with nothing faked.

    `RedisFlag` is the class the executor consults before every order that would open a position.
    It is constructed here over the same real client, on the same real key — so what this asserts
    is precisely "pressing the button stops the executor taking on new risk", minus only the
    executor process itself.
    """
    flag = RedisFlag(store)
    assert flag.engaged() is False

    client.post(ROUTE)

    assert flag.engaged() is True


def test_the_documented_release_works_against_a_real_redis(
    client: TestClient, store: redis.Redis
) -> None:
    """`redis-cli DEL executor:kill-switch` — the command the module docstring gives an operator.

    ⚠️ Deleting **one** key, as documented, while the stamp is left behind. If the switch were
    ever read as "either key present" this would fail, which is the point: the sibling stamp must
    not be able to keep a released switch alive.
    """
    client.post(ROUTE)
    assert RedisFlag(store).engaged() is True

    store.delete(SWITCH_KEY)

    assert RedisFlag(store).engaged() is False
    assert client.get(ROUTE).json() == {
        "engaged": False,
        "engaged_at": None,
        "layer": RedisFlag(store).name,
    }


def test_the_real_client_satisfies_the_protocol_at_runtime(store: redis.Redis) -> None:
    """Assignment proves it to mypy; calling every method proves it to Python.

    Two different claims. `SwitchStore` being satisfied is checked statically wherever a `Redis`
    is assigned to it, but a protocol can be structurally satisfied and still behave differently
    from what the caller assumed — `mset` taking a mapping rather than pairs, `exists` counting
    rather than answering a bool. Three calls, three shapes.
    """
    typed: SwitchStore = store

    assert typed.mset({SWITCH_KEY: ENGAGED_VALUE}) is True
    assert typed.exists(SWITCH_KEY) == 1
    assert typed.get(SWITCH_KEY) == ENGAGED_VALUE

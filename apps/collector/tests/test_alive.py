"""The host agent's heartbeat (`alive`): renewed while it runs, gone the moment it stops."""

import time
from typing import Any

import redis

from tradeforge_collector.alive import ALIVE_KEY, TTL_SECONDS, Heartbeat, alive_key


class _Redis:
    def __init__(self, *, fails: bool = False) -> None:
        self.sets: list[tuple[str, int]] = []
        self.deleted: list[str] = []
        self._fails = fails

    def set(self, name: str, value: Any, ex: int) -> None:
        if self._fails:
            raise redis.ConnectionError("gone")
        self.sets.append((name, ex))

    def delete(self, name: str) -> None:
        if self._fails:
            raise redis.ConnectionError("gone")
        self.deleted.append(name)


def test_the_key_is_written_at_start_and_expires_on_its_own() -> None:
    """⚠️ With an expiry: a process killed from the task manager runs no shutdown, and the key
    must still vanish — the expiry is what reports that death."""
    client = _Redis()
    beat = Heartbeat(client, every=60)  # type: ignore[arg-type]
    beat.start()
    beat.stop()

    assert client.sets[0] == (ALIVE_KEY, TTL_SECONDS)


def test_it_keeps_beating_from_its_own_thread() -> None:
    """⚠️ A thread, because `collect_range` holds the event loop for a whole download: a beat
    scheduled on the loop would go silent exactly while the agent is busiest."""
    client = _Redis()
    beat = Heartbeat(client, every=0.01)  # type: ignore[arg-type]
    beat.start()
    time.sleep(0.2)
    beat.stop()

    assert len(client.sets) >= 3


def test_a_clean_stop_clears_the_key_at_once() -> None:
    client = _Redis()
    beat = Heartbeat(client, every=60)  # type: ignore[arg-type]
    beat.start()
    beat.stop()

    assert client.deleted == [ALIVE_KEY]


def test_a_redis_that_does_not_answer_does_not_stop_the_agent() -> None:
    """The supervisor's watchdog is what reacts to Redis being gone; the heartbeat only logs."""
    beat = Heartbeat(_Redis(fails=True), every=60)  # type: ignore[arg-type]
    beat.start()
    beat.stop()


def test_a_brokers_agent_renews_its_own_key_beside_the_shared_one() -> None:
    """ADR-0032: the shared key answers "is anybody there?", the broker's says which; stopping
    clears only its own — another broker's agent may still be renewing the shared key."""
    client = _Redis()
    beat = Heartbeat(client, every=60, broker="tradeview")  # type: ignore[arg-type]
    beat.start()
    beat.stop()

    assert {name for name, _ in client.sets} == {ALIVE_KEY, alive_key("tradeview")}
    assert client.deleted == [alive_key("tradeview")]

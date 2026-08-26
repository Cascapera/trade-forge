"""The three layers, and the one property that matters more than all of them: they fail closed.

A kill switch is only worth what its worst day is worth. Every test here that says "engaged"
when something is *broken* rather than when something is *set* is the point of the file — a
layer that answers "not engaged" because it could not tell is a comment, not a switch.
"""

from pathlib import Path

import pytest
from redis import Redis
from redis.typing import KeyT

from tradeforge_executor.kill_switch import (
    SWITCH_KEY,
    EndpointFlag,
    FileFlag,
    FlagStore,
    RedisFlag,
)


class FakeStore:
    """Redis, as far as this module is concerned: one `exists`. Can be told to be broken."""

    def __init__(self, *, present: bool = False, broken: bool = False) -> None:
        self.present = present
        self.broken = broken
        self.asked = 0

    def exists(self, *names: KeyT) -> int:
        self.asked += 1
        if self.broken:
            raise ConnectionError("redis is not answering")
        return 1 if self.present else 0


def test_the_redis_flag_is_engaged_while_the_key_is_there() -> None:
    assert RedisFlag(FakeStore(present=True)).engaged() is True
    assert RedisFlag(FakeStore(present=False)).engaged() is False


def test_a_redis_that_cannot_be_asked_counts_as_engaged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """⚠️ **The whole file in one test.** An unreachable Redis is exactly the situation in which
    somebody may have been trying to engage the switch and failed to be heard — so the honest
    reading of "I do not know" is "stop", not "carry on".

    A layer that returned `False` here would pass every other test in this file.
    """
    with caplog.at_level("CRITICAL"):
        assert RedisFlag(FakeStore(broken=True)).engaged() is True


def test_the_redis_flag_is_read_every_time_it_is_asked() -> None:
    """⚠️ A cached answer is a switch that stays open for as long as the cache lives. The point
    of a kill switch is that it takes effect *now*."""
    store = FakeStore(present=False)
    flag = RedisFlag(store)

    assert flag.engaged() is False
    store.present = True

    assert flag.engaged() is True, "the answer was cached"
    assert store.asked == 2


def test_the_file_flag_follows_the_file(tmp_path: Path) -> None:
    """`touch` engages it and `rm` releases it — from any shell, with no client and no parsing.
    That is the layer that survives Redis being gone."""
    path = tmp_path / "KILL"
    flag = FileFlag(path)

    assert flag.engaged() is False
    path.touch()
    assert flag.engaged() is True
    path.unlink()
    assert flag.engaged() is False


def test_a_path_that_cannot_be_interrogated_counts_as_engaged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A dead mount or a permission error is "I do not know", and this file's whole doctrine is
    that "I do not know" means stop."""

    def refuse(_self: Path) -> bool:
        raise OSError("the mount is gone")

    monkeypatch.setattr(Path, "exists", refuse)

    with caplog.at_level("CRITICAL"):
        assert FileFlag(tmp_path / "KILL").engaged() is True


def test_the_endpoint_flag_starts_clear_and_engages_once_told(
    caplog: pytest.LogCaptureFixture,
) -> None:
    flag = EndpointFlag()
    assert flag.engaged() is False

    with caplog.at_level("CRITICAL"):
        flag.engage()

    assert flag.engaged() is True


def test_engaging_the_endpoint_is_one_way() -> None:
    """⚠️ Deliberately no `release()`. Coming back from a kill is an operator decision made with
    a running system in front of them — an endpoint that can un-kill is an endpoint a retry loop
    can un-kill."""
    assert not hasattr(EndpointFlag(), "release")
    assert not hasattr(EndpointFlag(), "disengage")


def test_each_layer_names_itself(tmp_path: Path) -> None:
    """The name goes into the audit log with the refusal. "Refused: kill switch" leaves an
    operator unable to tell whether somebody pulled the handle or Redis fell over, and those
    have opposite responses."""
    assert SWITCH_KEY in RedisFlag(FakeStore()).name
    assert "redis" in RedisFlag(FakeStore()).name
    assert "KILL" in FileFlag(tmp_path / "KILL").name
    assert EndpointFlag().name == "endpoint"


def test_the_real_redis_client_satisfies_the_flag_store() -> None:
    """⚠️ **Proved by assignment, so mypy checks it.** A `Protocol` nothing real is ever assigned
    to describes an imaginary client, and this one got it wrong in *both* directions on the first
    try — `names: str` was too narrow (a protocol is satisfied by a **wider** parameter) and
    `-> int` was too narrow as well (satisfied by a **narrower** return, and `Redis.exists` is
    declared `ResponseT`).

    Neither showed up until the real client was passed in. No connection is opened: constructing
    a `Redis` does not dial anything, and the assignment is a type-level statement.
    """
    client = Redis(host="localhost", port=6379)

    store: FlagStore = client

    assert store is client

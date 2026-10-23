"""The snapshots' child pool survives a child killed mid-computation (15/10) — no database."""

import os
from collections.abc import Callable
from concurrent.futures import Executor, Future
from concurrent.futures.process import BrokenProcessPool
from typing import Any

import pytest

from tradeforge_api.snapshots import ChildPool


def test_a_child_killed_mid_computation_does_not_break_the_next_one() -> None:
    """The real pool. `os._exit` ends the child as the kernel's OOM killer did on 15/10: no
    exception, no cleanup. Before, every call after it raised `BrokenProcessPool` at once."""
    pool = ChildPool()
    try:
        with pytest.raises(BrokenProcessPool):
            pool.run(os._exit, 1)
        child = pool.run(os.getpid)
        assert isinstance(child, int)
        assert child != os.getpid()
    finally:
        pool.shutdown()


class _Pool(Executor):
    """A pool that runs in the caller, or answers as a broken one."""

    def __init__(self, *, broken: bool) -> None:
        self.broken = broken
        self.shut = False

    def submit(self, fn: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Future[Any]:
        if self.broken:
            raise BrokenProcessPool("a child process terminated abruptly")
        done: Future[Any] = Future()
        try:
            done.set_result(fn(*args, **kwargs))
        except Exception as exc:  # noqa: BLE001 — handed to the caller, as a real future does
            done.set_exception(exc)
        return done

    def shutdown(self, wait: bool = True, *, cancel_futures: bool = False) -> None:
        self.shut = True


def test_a_broken_pool_is_shut_and_replaced_and_the_error_goes_on_up() -> None:
    made = [_Pool(broken=True), _Pool(broken=False)]
    handed = iter(made)
    pool = ChildPool(make=lambda: next(handed))

    with pytest.raises(BrokenProcessPool):
        pool.run(len, "abc")

    assert made[0].shut
    assert pool.run(len, "abc") == 3


def test_an_error_of_the_computation_keeps_the_pool() -> None:
    """Only a broken pool is replaced: a computation that raised leaves a pool that works."""
    made: list[_Pool] = []

    def make() -> _Pool:
        made.append(_Pool(broken=False))
        return made[-1]

    pool = ChildPool(make=make)
    with pytest.raises(ValueError, match="invalid literal"):
        pool.run(int, "not a number")

    assert len(made) == 1
    assert not made[0].shut

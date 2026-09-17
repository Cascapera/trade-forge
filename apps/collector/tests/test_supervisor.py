"""The agent's lifeline on fakes: what ends it, what it survives, and what stays loud."""

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from arq.connections import RedisSettings
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError

from tradeforge_collector import supervisor
from tradeforge_collector.lifetime import EXIT_NO_REDIS
from tradeforge_collector.supervisor import redis_answers, serve, silent_for, wait_for_redis

NOWHERE = RedisSettings(host="127.0.0.1", port=1)
SETTINGS: dict[str, Any] = {}
"""What arq would be handed. The fake factory never reads it."""

FOREVER = object()
"""A worker ending that never comes: `main` runs until it is cancelled."""


class _FakeWorker:
    """Ends its `main` the way the script says, and records how it was stopped and closed."""

    def __init__(self, ending: object, *, close_hangs: bool = False) -> None:
        self.ending = ending
        self.close_hangs = close_hangs
        self.closed = False
        self.cancelled = False
        self.main_task: asyncio.Task[None] | None = None

    async def main(self) -> None:
        if self.ending is FOREVER:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled = True
                raise
        if isinstance(self.ending, BaseException):
            raise self.ending

    async def close(self) -> None:
        self.closed = True
        if self.close_hangs:
            await asyncio.Event().wait()  # a silent Redis: arq's close never returns
        # arq's own close fails when the server is gone (on 16/09 it was a redis TimeoutError;
        # both kinds are in REDIS_GONE).
        raise RedisConnectionError("Connection closed by server")


class _Script:
    """The workers to hand out, in order, and every one made so far."""

    def __init__(self, *endings: object, close_hangs: bool = False) -> None:
        self.endings = iter(endings)
        self.close_hangs = close_hangs
        self.made: list[_FakeWorker] = []

    def __call__(self, _settings: object) -> _FakeWorker:
        self.made.append(_FakeWorker(next(self.endings), close_hangs=self.close_hangs))
        return self.made[-1]


class _Redis:
    """Answers with `up`, and counts the questions."""

    def __init__(self, *, up: bool = True) -> None:
        self.up = up
        self.asked = 0

    async def __call__(self) -> bool:
        self.asked += 1
        await asyncio.sleep(0)
        return self.up


def serving(
    make: Callable[[object], _FakeWorker],
    answers: Callable[[], Awaitable[bool]],
    *,
    grace: float = 0,
) -> int:
    return asyncio.run(
        serve(
            SETTINGS,
            redis_settings=NOWHERE,
            grace=grace,
            every=0,
            make_worker=make,
            answers=answers,
        )
    )


class TestStarting:
    def test_a_redis_that_does_not_answer_at_start_is_a_setup_to_fix(self) -> None:
        # Docker not up, or a wrong REDIS_PORT: at once, not after arq's retries and the grace,
        # and not with the 0 that a clean stop returns.
        script = _Script()

        assert serving(script, _Redis(up=False)) == EXIT_NO_REDIS == 2
        assert script.made == []


class TestALostRedis:
    @pytest.mark.parametrize(
        "gone", [RedisConnectionError("refused"), RedisTimeoutError("connect")]
    )
    def test_a_worker_that_crashed_on_it_ends_the_agent_once_the_grace_passes(
        self, gone: BaseException
    ) -> None:
        script = _Script(gone)
        redis = _Redis()

        async def answers() -> bool:
            # Up at start; down from the moment the worker has crashed.
            if script.made:
                redis.up = False
            return await redis()

        assert serving(script, answers) == 0
        assert len(script.made) == 1
        # Closed, although closing talks to the missing server — and that failure swallowed.
        assert script.made[0].closed

    def test_a_redis_that_comes_back_gets_a_fresh_worker(self) -> None:
        # The first worker crashes on a restart; Redis answers again; the second ends normally.
        script = _Script(RedisConnectionError("restart"), None)

        assert serving(script, _Redis(), grace=5) == 0
        assert len(script.made) == 2
        assert all(worker.closed for worker in script.made)

    def test_a_silent_redis_is_noticed_while_the_worker_is_still_waiting_on_it(self) -> None:
        # A paused container: arq raises nothing and its worker would wait for ever. The watchdog
        # is what ends it — the worker is cancelled, not left running.
        script = _Script(FOREVER)
        redis = _Redis()

        async def answers() -> bool:
            if script.made:
                redis.up = False
            return await redis()

        assert serving(script, answers) == 0
        assert script.made[0].cancelled
        assert script.made[0].closed

    def test_a_close_that_never_returns_does_not_hold_the_agent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(supervisor, "CLOSE_TIMEOUT", 0.01)
        script = _Script(FOREVER, close_hangs=True)
        redis = _Redis()

        async def answers() -> bool:
            if script.made:
                redis.up = False
            return await redis()

        assert serving(script, answers) == 0
        assert script.made[0].closed

    def test_the_watchdog_leaves_a_redis_that_answers_alone(self) -> None:
        # With Redis up, only the worker's own ending ends the agent.
        script = _Script(None)
        redis = _Redis()

        assert serving(script, redis) == 0
        assert len(script.made) == 1
        assert not script.made[0].cancelled


class TestWhatStaysLoud:
    @pytest.mark.parametrize("bug", [ValueError("a real bug"), FileNotFoundError("parquet")])
    def test_an_error_that_is_not_a_lost_redis_propagates(self, bug: BaseException) -> None:
        # A file error is an OSError; counting it as "Redis went away" would bury a bug.
        script = _Script(bug)

        with pytest.raises(type(bug)):
            serving(script, _Redis())

        assert script.made[0].closed


class TestStopping:
    def test_a_stop_while_serving_ends_the_worker_and_returns_zero(self) -> None:
        script = _Script(FOREVER)

        async def scenario() -> int:
            task = asyncio.ensure_future(
                serve(
                    SETTINGS,
                    redis_settings=NOWHERE,
                    grace=60,
                    every=0,
                    make_worker=script,
                    answers=_Redis(),
                )
            )
            while not script.made:
                await asyncio.sleep(0)
            task.cancel()
            return await task

        assert asyncio.run(scenario()) == 0
        assert script.made[0].cancelled
        assert script.made[0].closed

    def test_a_stop_during_the_grace_returns_zero_too(self) -> None:
        script = _Script(RedisConnectionError("gone"))
        redis = _Redis()

        async def scenario() -> int:
            async def answers() -> bool:
                if script.made:
                    redis.up = False
                return await redis()

            task = asyncio.ensure_future(
                serve(
                    SETTINGS,
                    redis_settings=NOWHERE,
                    grace=60,
                    every=0.01,
                    make_worker=script,
                    answers=answers,
                )
            )
            # Wait until the worker has crashed and the grace is being counted.
            while redis.asked < 5:
                await asyncio.sleep(0.01)
            task.cancel()
            return await task

        assert asyncio.run(scenario()) == 0

    def test_the_task_it_awaits_is_the_one_arq_would_cancel(self) -> None:
        # arq's signal handler (where the platform has one) cancels `main_task`. A worker that
        # cancels its own `main_task` must therefore end the agent.
        class _SelfCancelling(_FakeWorker):
            async def main(self) -> None:
                assert self.main_task is not None
                self.main_task.cancel()
                await asyncio.sleep(1)

        worker = _SelfCancelling(None)

        assert serving(lambda _settings: worker, _Redis()) == 0
        assert worker.closed


class TestWaitForRedis:
    def test_an_answer_ends_the_wait_at_once(self) -> None:
        slept: list[float] = []

        async def sleep(seconds: float) -> None:
            slept.append(seconds)

        assert asyncio.run(wait_for_redis(_Redis(), grace=10, every=1, sleep=sleep))
        assert slept == []

    def test_it_keeps_asking_until_the_grace_runs_out(self) -> None:
        now = [0.0]
        asked: list[float] = []

        async def ask() -> bool:
            asked.append(now[0])
            return False

        async def sleep(seconds: float) -> None:
            now[0] += seconds

        came_back = asyncio.run(
            wait_for_redis(ask, grace=3, every=1, clock=lambda: now[0], sleep=sleep)
        )

        assert not came_back
        # Asked at 0, 1, 2 and 3 — once more at the deadline itself, never past it.
        assert asked == [0.0, 1.0, 2.0, 3.0]


class _Clock:
    """A clock that only moves when slept on, and a Redis that answers by the time."""

    def __init__(self, up: Callable[[float], bool], *, rounds: int = 100) -> None:
        self.now = 0.0
        self.up = up
        self.rounds = rounds
        self.asked: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def ask(self) -> bool:
        self.asked.append(self.now)
        return self.up(self.now)

    async def sleep(self, seconds: float) -> None:
        self.rounds -= 1
        if self.rounds < 0:
            raise AssertionError("silent_for never returned")
        self.now += seconds


def watching(clock: _Clock, grace: float) -> None:
    asyncio.run(silent_for(clock.ask, grace=grace, every=1, clock=clock, sleep=clock.sleep))


class TestSilentFor:
    def test_it_ends_after_grace_seconds_without_an_answer_in_a_row(self) -> None:
        # Answers at 0 and 1, silent from 2: the silence is counted from the last answer, at 1,
        # so it ends at 4 — not at 3, counting from the start.
        clock = _Clock(lambda now: now < 2)

        watching(clock, grace=3)

        assert clock.now == 4.0

    def test_an_answer_resets_the_count(self) -> None:
        # Up only at 2: the count restarts there, so a grace of 2 ends at 4, not at 2.
        clock = _Clock(lambda now: now == 2)

        watching(clock, grace=2)

        assert clock.now == 4.0

    def test_a_miss_is_asked_again_before_it_counts(self) -> None:
        # Silent from the start: every round asks twice before the silence may count.
        clock = _Clock(lambda _now: False)

        watching(clock, grace=1)

        assert clock.asked == [0.0, 0.0, 1.0, 1.0]

    def test_a_ping_that_timed_out_behind_a_long_job_is_not_silence(self) -> None:
        # The first ask of each round fails — it was waiting while a synchronous collection held
        # the loop — and the second, asked once the loop is free, succeeds. Redis is up; the
        # agent must not stop, however long the rounds run.
        clock = _Clock(lambda _now: True, rounds=20)
        asked_at: list[float] = []

        async def stale_then_fresh() -> bool:
            # False the first time in a round, True when asked again in the same round.
            fresh = clock.now in asked_at
            asked_at.append(clock.now)
            return fresh

        with pytest.raises(AssertionError, match="never returned"):
            asyncio.run(
                silent_for(stale_then_fresh, grace=3, every=1, clock=clock, sleep=clock.sleep)
            )
        assert clock.now == 20.0


class TestTheWatchdogsOwnErrors:
    def test_an_error_that_is_not_silence_is_raised_not_taken_for_a_stopped_stack(self) -> None:
        # A ping that fails for another reason (a refused command, say) is a problem to read.
        script = _Script(FOREVER)
        calls = [0]

        async def answers() -> bool:
            calls[0] += 1
            if calls[0] == 1:
                return True  # the start-up check
            raise RuntimeError("the ping itself is broken")

        with pytest.raises(RuntimeError, match="ping itself"):
            serving(script, answers)

        # And the worker is not left running behind the error.
        assert script.made[0].cancelled
        assert script.made[0].closed


class TestAStopIsNeverLost:
    def test_a_cancellation_aimed_at_the_caller_survives_waiting_for_a_task(self) -> None:
        # While the caller waits for a task to finish stopping, the caller itself is cancelled.
        # That request must come through — before, `_stop` swallowed it and the agent went on.
        async def slow_to_stop() -> None:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await asyncio.sleep(0.05)
                raise

        async def scenario() -> None:
            inner = asyncio.ensure_future(slow_to_stop())
            await asyncio.sleep(0)
            outer = asyncio.ensure_future(supervisor._stop(inner))
            await asyncio.sleep(0.01)
            outer.cancel()
            await asyncio.wait_for(outer, timeout=5)

        with pytest.raises(asyncio.CancelledError):
            asyncio.run(scenario())

    def test_stopping_a_task_is_quiet_when_nobody_cancelled_the_caller(self) -> None:
        async def scenario() -> bool:
            inner = asyncio.ensure_future(asyncio.Event().wait())
            await asyncio.sleep(0)
            await supervisor._stop(inner)
            return inner.cancelled()

        assert asyncio.run(scenario())


def test_a_port_nobody_listens_on_is_a_no_not_an_exception() -> None:
    assert asyncio.run(redis_answers(NOWHERE)) is False


def test_a_sentinel_configuration_is_refused() -> None:
    with pytest.raises(TypeError, match="single host"):
        asyncio.run(redis_answers(RedisSettings(host=[("a", 1)])))


def test_the_ping_reaches_redis_the_way_the_worker_does(monkeypatch: pytest.MonkeyPatch) -> None:
    # A Redis with a password, or on another database, must not look absent to the watchdog
    # while the worker talks to it happily.
    seen: dict[str, object] = {}

    class _Client:
        def __init__(self, **kwargs: object) -> None:
            seen.update(kwargs)

        async def ping(self) -> bool:
            return True

        async def aclose(self) -> None:
            return None

    monkeypatch.setattr(supervisor, "Redis", _Client)
    settings = RedisSettings(
        host="redis.local", port=6380, database=3, username="agent", password="s3cret", ssl=True
    )

    assert asyncio.run(redis_answers(settings)) is True
    assert seen == {
        "host": "redis.local",
        "port": 6380,
        "db": 3,
        "username": "agent",
        "password": "s3cret",
        "ssl": True,
        "socket_connect_timeout": 1,
        "socket_timeout": 1,
    }

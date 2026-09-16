"""Keep the host agent running exactly as long as the stack it serves.

`tradeforge-collector agent` runs the arq worker from `agent.WorkerSettings` under this loop.
The agent is only useful while Docker is up — its jobs come from the stack's Redis and its
results go to the stack's Postgres — so it should not outlive them.

⚠️ **An exit on a lost Redis already happened before this module, and only by accident.**
Measured on 16/09 by cutting a proxy between the agent and Redis: `arq` crashed three seconds
later, when `Worker.close` tried to delete its health-check key on a server that was gone, and
exited with a traceback and status 1. Nothing promised that — and a Redis that stops answering
*without* closing the connection (a paused container, a suspended VM, a sleeping laptop) raised
nothing at all: arq's pool has no socket timeout, and the agent waited for ever.

So the rule is enforced here, not inferred from arq: a **watchdog** pings Redis (with its own
one-second timeout) beside the worker, and when nothing has answered for `grace` seconds the
worker is cancelled and the agent exits with status 0. A worker that crashes on a lost Redis
gets the same `grace` to see it come back, and a fresh worker if it does.

⚠️ **A collection in progress is not interrupted, and must not look like silence.** The job runs
synchronously and blocks the event loop for as long as it takes — minutes, for a cold history.
A ping that was on the wire when the job started times out the moment the loop wakes, however
healthy Redis is, so `silent_for` asks again at once before it counts a miss: only a ping that
fails *after* the loop is free again says anything about Redis. With the stack genuinely down,
the job's next write to Postgres fails, the job ends, and that second ping fails too.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, Protocol

from arq.connections import RedisSettings
from arq.worker import create_worker
from redis.asyncio import Redis
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError

from tradeforge_collector.lifetime import DEFAULT_GRACE, EXIT_NO_REDIS

if TYPE_CHECKING:
    from arq.typing import WorkerSettingsType

__all__ = [
    "CLOSE_TIMEOUT",
    "DEFAULT_GRACE",
    "EXIT_NO_REDIS",
    "REDIS_GONE",
    "redis_answers",
    "serve",
    "silent_for",
    "wait_for_redis",
]

logger = logging.getLogger(__name__)

REDIS_GONE: tuple[type[BaseException], ...] = (RedisConnectionError, RedisTimeoutError)
"""What a Redis that is not there looks like from here. redis-py turns socket failures into
these two; the one measured on 16/09, raised from `Worker.close`, was the second. Deliberately
not `OSError`: a file error inside the agent is a bug, and it must stay loud."""

CLOSE_TIMEOUT = 5.0
"""Seconds `Worker.close` may take. It waits for running jobs and then talks to Redis, and
against a silent server that second part would otherwise wait for ever."""


class _Worker(Protocol):
    main_task: asyncio.Task[None] | None

    async def main(self) -> None: ...

    async def close(self) -> None: ...


Ask = Callable[[], Awaitable[bool]]
Clock = Callable[[], float]
Sleep = Callable[[float], Awaitable[Any]]


async def redis_answers(settings: RedisSettings) -> bool:
    """One short ping to the worker's own Redis. A server that is not there is `False`.

    Anything else propagates: a Sentinel configuration is refused outright, and an error that is
    not a missing server is a problem to read, not a reason to call the stack down.
    """
    if not isinstance(settings.host, str):
        # A Sentinel list: the agent is configured from REDIS_HOST, which is always one host.
        raise TypeError("the agent reaches Redis at a single host, not through Sentinel")
    client = Redis(
        host=settings.host,
        port=settings.port,
        db=settings.database,
        username=settings.username,
        password=settings.password,
        ssl=settings.ssl,
        socket_connect_timeout=1,
        socket_timeout=1,
    )
    try:
        return bool(await client.ping())
    except REDIS_GONE:
        return False
    finally:
        with contextlib.suppress(*REDIS_GONE):
            await client.aclose()


async def wait_for_redis(
    answers: Ask,
    *,
    grace: float,
    every: float,
    clock: Clock = time.monotonic,
    sleep: Sleep = asyncio.sleep,
) -> bool:
    """Ask until Redis answers or `grace` seconds have passed. It is always asked at least once."""
    deadline = clock() + grace
    while True:
        if await answers():
            return True
        if clock() >= deadline:
            return False
        await sleep(every)


async def silent_for(
    answers: Ask,
    *,
    grace: float,
    every: float,
    clock: Clock = time.monotonic,
    sleep: Sleep = asyncio.sleep,
) -> None:
    """Return once Redis has not answered for `grace` seconds in a row. Runs until then.

    ⚠️ A miss is confirmed by an immediate second ask. The first may have been waiting while a
    synchronous job held the loop, and its timeout then says nothing about Redis — see the
    module's note.
    """
    last_answer = clock()
    while True:
        if await answers() or await answers():
            last_answer = clock()
        elif clock() - last_answer >= grace:
            return
        await sleep(every)


async def _close_quietly(worker: _Worker) -> None:
    """Close the worker without letting a missing or silent Redis turn that into a crash or a hang.

    arq's `close` deletes its health-check key before it closes its pool, so against a lost
    server it fails early and leaves that pool open. Nothing is gained by chasing it: the process
    is about to either exit or start a worker with a pool of its own.
    """
    with contextlib.suppress(*REDIS_GONE, TimeoutError):
        await asyncio.wait_for(worker.close(), timeout=CLOSE_TIMEOUT)


async def _stop(task: asyncio.Future[Any]) -> None:
    """Cancel `task` and wait for it — without swallowing a cancellation aimed at *us*."""
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        current = asyncio.current_task()
        if current is not None and current.cancelling():
            raise
    except REDIS_GONE:
        pass


async def _end(worker: _Worker, *tasks: asyncio.Future[Any]) -> None:
    """Stop whatever of a life is still running and close its worker. Safe to repeat."""
    for task in tasks:
        if not task.done():
            await _stop(task)
    await _close_quietly(worker)


async def serve(  # noqa: PLR0913 — two of these are the seams the tests stand on
    settings: WorkerSettingsType,
    *,
    redis_settings: RedisSettings,
    grace: float = DEFAULT_GRACE,
    every: float = 1.0,
    make_worker: Callable[[WorkerSettingsType], _Worker] = create_worker,
    answers: Ask | None = None,
) -> int:
    """Run the agent until it is stopped or Redis stays unanswered for `grace` seconds.

    Returns 0 for either, and `EXIT_NO_REDIS` when Redis does not answer at start — a stack
    that is not up, or a wrong address, which is a setup to fix rather than a clean stop. A
    fresh worker per life, so each one starts from a pool it opened itself. Any error that is
    not a lost Redis propagates — from the worker or from the watchdog — because a bug in the
    agent must stay loud.
    """
    ask = answers or (lambda: redis_answers(redis_settings))
    if not await ask():
        logger.error(
            "Redis does not answer at %s:%s — start the stack first",
            redis_settings.host,
            redis_settings.port,
        )
        return EXIT_NO_REDIS

    while True:
        worker = make_worker(settings)
        main = asyncio.ensure_future(worker.main())
        worker.main_task = main
        watchdog = asyncio.ensure_future(silent_for(ask, grace=grace, every=every))
        try:
            outcome = await _one_life(worker, main, watchdog, grace=grace, every=every, ask=ask)
        except asyncio.CancelledError:
            # Ctrl-C (asyncio.run cancels this task), or whoever runs this cancelling it — at
            # any point of a life, the grace included. The request is honoured by returning, so
            # it is withdrawn first: otherwise `_stop` would take it for a second one and cut
            # the cleanup short.
            current = asyncio.current_task()
            if current is not None:
                current.uncancel()
            await _end(worker, watchdog, main)
            logger.info("agent stopped")
            return 0
        except Exception:
            await _end(worker, watchdog, main)
            raise
        if outcome is not None:
            return outcome
        logger.info("Redis is back; the agent carries on")


async def _one_life(  # noqa: PLR0913 — the state of one life, passed rather than stored
    worker: _Worker,
    main: asyncio.Future[None],
    watchdog: asyncio.Future[None],
    *,
    grace: float,
    every: float,
    ask: Ask,
) -> int | None:
    """One worker, from start to its end. An exit status, or `None` to start another."""
    await asyncio.wait({main, watchdog}, return_when=asyncio.FIRST_COMPLETED)

    if watchdog.done():
        watchdog.result()  # the watchdog's own bug, if any, is raised here and not taken as silence
        await _stop(main)
        await _close_quietly(worker)
        logger.info(
            "Redis has not answered for %ss — the stack is down, so the agent stops too", grace
        )
        return 0

    await _stop(watchdog)
    failure = None if main.cancelled() else main.exception()
    await _close_quietly(worker)
    if failure is None:
        logger.info("agent stopped")
        return 0
    if not isinstance(failure, REDIS_GONE):
        raise failure

    logger.warning(
        "lost Redis (%s); waiting up to %ss for it to come back",
        failure.__class__.__name__,
        grace,
    )
    if not await wait_for_redis(ask, grace=grace, every=every):
        logger.info(
            "Redis has been gone for %ss — the stack is down, so the agent stops too", grace
        )
        return 0
    return None

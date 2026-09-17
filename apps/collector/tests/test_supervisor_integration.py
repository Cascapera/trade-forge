"""The agent against a real Redis that disappears, through a proxy this test controls.

The case measured on 16/09, turned into tests: without the supervisor, arq crashed with a
traceback three seconds after the proxy closed, and did nothing at all when the proxy went
silent instead. With it, the agent must end with status 0 once the grace has passed, and must
take work again when the server comes back inside it.

⚠️ **The agent here drains a queue of its own**, never `collect`. The Redis this runs against on
a developer's machine is the stack's, and a worker on the real queue would take real collection
jobs and try to talk to MetaTrader.
"""

import asyncio
import contextlib
import os
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from arq import create_pool
from arq.connections import RedisSettings

from tradeforge_collector.lifetime import EXIT_NO_REDIS
from tradeforge_collector.supervisor import serve

pytestmark = pytest.mark.integration

REDIS_HOST = os.environ.get("REDIS_HOST", "localhost")
REDIS_PORT = int(os.environ.get("REDIS_PORT", "6379"))


class _Proxy:
    """Forwards TCP to Redis. `cut` closes it all; `freeze` keeps sockets open, answers nothing."""

    def __init__(self) -> None:
        self.server: asyncio.Server | None = None
        self.writers: list[asyncio.StreamWriter] = []
        self.port = 0
        self.frozen = False

    async def start(self, port: int = 0) -> None:
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", port)
        self.port = self.server.sockets[0].getsockname()[1]

    async def cut(self) -> None:
        assert self.server is not None
        self.server.close()
        for writer in self.writers:
            writer.close()
        self.writers.clear()
        await self.server.wait_closed()

    def freeze(self) -> None:
        self.frozen = True

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        up_reader, up_writer = await asyncio.open_connection(REDIS_HOST, REDIS_PORT)
        self.writers.extend([writer, up_writer])
        await asyncio.gather(self._pipe(reader, up_writer), self._pipe(up_reader, writer))

    async def _pipe(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        with contextlib.suppress(Exception):
            while data := await reader.read(65536):
                if self.frozen:
                    continue  # swallowed: the connection stays open and nothing comes back
                writer.write(data)
                await writer.drain()
        writer.close()


async def _mark(ctx: dict[str, Any], key: str) -> None:
    """A job that leaves a trace: proof that the agent is taking work, whichever worker ran it."""
    await ctx["redis"].set(key, "done", ex=60)


def _settings(port: int) -> Any:  # an arq settings class, which has no type of its own
    class Settings:
        functions = (_mark,)
        queue_name = f"collect-supervisor-test-{uuid.uuid4()}"
        redis_settings = RedisSettings(host="127.0.0.1", port=port, conn_retries=0)
        poll_delay = 0.1
        # `handle_signals` left at arq's default, as the agent has it: turned off, arq's close
        # reaches for `signal.SIGUSR1`, which Windows does not have.

    return Settings


async def _until(condition: Callable[[], Awaitable[Any]], timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if await condition():
            return True
        await asyncio.sleep(0.1)
    return False


def test_the_agent_ends_cleanly_when_the_stack_goes_away() -> None:
    async def scenario() -> int:
        proxy = _Proxy()
        await proxy.start()
        settings = _settings(proxy.port)
        agent = asyncio.ensure_future(
            serve(settings, redis_settings=settings.redis_settings, grace=1.0, every=0.2)
        )
        await asyncio.sleep(1.0)
        assert not agent.done(), "the agent ended before Redis went away"

        await proxy.cut()

        return await asyncio.wait_for(agent, timeout=15)

    assert asyncio.run(scenario()) == 0


def test_the_agent_ends_when_redis_goes_silent_without_closing() -> None:
    # A paused container or a suspended VM: the sockets stay open and nothing answers. arq alone
    # waited for ever here (measured); the watchdog must end it.
    async def scenario() -> int:
        proxy = _Proxy()
        await proxy.start()
        settings = _settings(proxy.port)
        agent = asyncio.ensure_future(
            serve(settings, redis_settings=settings.redis_settings, grace=2.0, every=0.2)
        )
        await asyncio.sleep(1.0)
        assert not agent.done(), "the agent ended before Redis went silent"

        proxy.freeze()

        try:
            return await asyncio.wait_for(agent, timeout=20)
        finally:
            await proxy.cut()

    assert asyncio.run(scenario()) == 0


def test_the_agent_takes_work_again_when_redis_comes_back_inside_the_grace() -> None:
    # Proved by work done after the comeback, not by the agent merely still running. Which
    # worker does it is not the promise: a first worker whose client reconnects before it ever
    # notices the gap is as good as a fresh one — and a health-check key cannot tell them apart,
    # which is what made the first version of this test fail three times in eight.
    async def scenario() -> tuple[bool, bool]:
        proxy = _Proxy()
        await proxy.start()
        settings = _settings(proxy.port)
        # Not through the proxy: the test's own view of Redis never goes away.
        queue = await create_pool(
            RedisSettings(host=REDIS_HOST, port=REDIS_PORT),
            default_queue_name=settings.queue_name,
        )
        before, after = (f"{settings.queue_name}:{name}" for name in ("before", "after"))
        agent = asyncio.ensure_future(
            serve(settings, redis_settings=settings.redis_settings, grace=10.0, every=0.2)
        )
        try:
            await queue.enqueue_job("_mark", before)
            worked_before = await _until(lambda: queue.exists(before), timeout=5)

            await proxy.cut()
            await asyncio.sleep(1.0)
            await proxy.start(proxy.port)  # the same port, as a restarted container would be

            await queue.enqueue_job("_mark", after)
            worked_after = await _until(lambda: queue.exists(after), timeout=8)
            return worked_before, worked_after and not agent.done()
        finally:
            agent.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.wait_for(agent, timeout=10)
            await proxy.cut()
            await queue.delete(before, after, settings.queue_name)
            await queue.aclose()

    worked_before, worked_after = asyncio.run(scenario())
    assert worked_before, "the agent never took a job before the gap, so this proves nothing"
    assert worked_after, "the agent took no work after Redis came back"


def test_an_agent_started_without_redis_says_so_at_once() -> None:
    async def scenario() -> tuple[int, float]:
        proxy = _Proxy()
        await proxy.start()
        port = proxy.port
        await proxy.cut()  # nothing listens there any more
        settings = _settings(port)
        started = time.monotonic()
        code = await asyncio.wait_for(
            serve(settings, redis_settings=settings.redis_settings, grace=15.0), timeout=10
        )
        return code, time.monotonic() - started

    code, took = asyncio.run(scenario())
    assert code == EXIT_NO_REDIS
    # Not arq's connection retries plus the grace: 26 s, measured before this rule.
    assert took < 5

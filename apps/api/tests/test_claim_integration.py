"""`OneTripWorker` against a real Redis: one round trip per claim, and no job taken twice.

⚠️ **Why a real Redis and not a fake.** What is under test is a Lua script and arq's own keys:
that Redis runs the script whole, that `PSETEX` writes the key arq's `EXISTS` reads, that
`ZSCORE` hands back what `run_job` expects. A fake would agree with whatever it was written to
agree with. And arq's `Worker` is subclassed through methods it does not promise to keep
(`start_jobs`, `sem`, `job_counter`, `tasks`): running the real worker is what fails if an arq
upgrade moves them.

⚠️ **On database 15, with a queue of its own.** The sweep's queue is `arq:queue` on database 0;
nothing here touches either. Keys are named after a per-test token and deleted on the way in
and out, never by `flushdb` — other integration tests share database 15.

Run with:  docker compose up -d  &&  POSTGRES_DB=tradeforge_test uv run pytest -m integration
"""

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from arq import create_pool
from arq.connections import ArqRedis, RedisSettings
from arq.constants import in_progress_key_prefix
from arq.worker import Worker, func

from tradeforge_api.claim import OneTripWorker
from tradeforge_api.config import Settings

pytestmark = pytest.mark.integration

SANDBOX_DB = 15

Scenario = Callable[[ArqRedis, str], Awaitable[None]]


async def _noop(ctx: dict[str, Any]) -> None:
    return None


def _in_sandbox(settings: Settings, scenario: Scenario) -> None:
    """Run `scenario` with a pool on the sandbox database and a token nobody else uses.

    Every key it makes carries the token and is deleted before and after — never `flushdb`.
    """

    async def body() -> None:
        pool = await create_pool(
            RedisSettings(host=settings.redis_host, port=settings.redis_port, database=SANDBOX_DB)
        )
        token = f"claimtest-{uuid.uuid4().hex[:12]}"

        async def clean() -> None:
            keys = [key async for key in pool.scan_iter(match=f"*{token}*")]
            if keys:
                await pool.delete(*keys)

        await clean()
        try:
            await scenario(pool, token)
        finally:
            await clean()
            await pool.aclose()

    asyncio.run(body())


def _worker(pool: ArqRedis, queue: str) -> OneTripWorker:
    return OneTripWorker(
        functions=[func(_noop, name="noop")],
        redis_pool=pool,
        queue_name=queue,
        handle_signals=False,
    )


async def _enqueue(pool: ArqRedis, queue: str, token: str, count: int) -> list[str]:
    ids = [f"{token}-{n:03d}" for n in range(count)]
    for job_id in ids:
        assert await pool.enqueue_job("noop", _job_id=job_id, _queue_name=queue)
    return ids


def test_takes_the_first_job_nobody_holds_and_marks_it_held(settings: Settings) -> None:
    async def scenario(pool: ArqRedis, token: str) -> None:
        queue = f"{token}:queue"
        ids = await _enqueue(pool, queue, token, 5)
        for held in ids[:3]:
            await pool.set(in_progress_key_prefix + held, b"1")
        worker = _worker(pool, queue)

        taken = await worker.claim(ids)

        assert taken is not None
        assert taken[0] == ids[3]
        assert taken[1] == int(await pool.zscore(queue, ids[3]) or 0)
        # The same key and lifetime arq itself would set — which is what lets the two coexist.
        ttl_ms = await pool.pttl(in_progress_key_prefix + ids[3])
        assert 0 < ttl_ms <= int(worker.in_progress_timeout_s * 1000)
        assert not await pool.exists(in_progress_key_prefix + ids[4])

    _in_sandbox(settings, scenario)


def test_takes_nothing_when_every_job_is_held_gone_or_not_due(settings: Settings) -> None:
    async def scenario(pool: ArqRedis, token: str) -> None:
        queue = f"{token}:queue"
        held, gone = await _enqueue(pool, queue, token, 2)
        await pool.set(in_progress_key_prefix + held, b"1")
        await pool.zrem(queue, gone)
        later = f"{token}-later"
        assert await pool.enqueue_job("noop", _job_id=later, _queue_name=queue, _defer_by=3600)
        worker = _worker(pool, queue)

        assert await worker.claim([held, gone, later]) is None
        assert not await pool.exists(in_progress_key_prefix + later)

    _in_sandbox(settings, scenario)


def test_a_claim_past_thirty_held_jobs_is_one_round_trip(settings: Settings) -> None:
    """The whole point: the cost of a claim no longer grows with the jobs other workers hold."""

    async def scenario(pool: ArqRedis, token: str) -> None:
        queue = f"{token}:queue"
        ids = await _enqueue(pool, queue, token, 32)
        for held in ids[:30]:
            await pool.set(in_progress_key_prefix + held, b"1")
        worker = _worker(pool, queue)
        await worker.claim([f"{token}-absent"])  # loads the script into Redis once

        sent: list[Any] = []
        real: Callable[..., Awaitable[Any]] = pool.execute_command

        async def counted(*args: Any, **options: Any) -> Any:
            sent.append(args[0])
            return await real(*args, **options)

        pool.execute_command = counted  # type: ignore[method-assign]
        try:
            taken = await worker.claim(ids)
        finally:
            pool.execute_command = real  # type: ignore[method-assign,assignment]

        assert taken is not None
        assert taken[0] == ids[30]
        assert sent == ["EVALSHA"]

    _in_sandbox(settings, scenario)


def test_an_arq_worker_and_a_one_trip_worker_never_take_the_same_job(settings: Settings) -> None:
    """Machines are updated one at a time: old and new must share a queue without a double run."""

    async def scenario(pool: ArqRedis, token: str) -> None:
        queue = f"{token}:queue"
        ran: list[tuple[str, str]] = []

        async def record(ctx: dict[str, Any]) -> None:
            ran.append((ctx["name"], ctx["job_id"]))
            await asyncio.sleep(0.01)

        ids = [f"{token}-{n:03d}" for n in range(60)]
        for job_id in ids:
            assert await pool.enqueue_job("record", _job_id=job_id, _queue_name=queue)

        def make(cls: type[Worker], name: str) -> Worker:
            return cls(
                functions=[func(record, name="record")],
                redis_pool=pool,
                queue_name=queue,
                handle_signals=False,
                burst=True,
                max_jobs=1,
                poll_delay=0.001,
                keep_result=0,
                ctx={"name": name},
            )

        old, new = make(Worker, "arq"), make(OneTripWorker, "one-trip")
        await asyncio.wait_for(asyncio.gather(old.main(), new.main()), timeout=60)

        assert sorted(job_id for _, job_id in ran) == ids
        assert {name for name, _ in ran} == {"arq", "one-trip"}

    _in_sandbox(settings, scenario)

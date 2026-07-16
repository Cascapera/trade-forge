"""The async job queue (arq over Redis) and the progress channel.

The API never runs a backtest — it drops a job here and returns `202`. A separate worker
process (`arq tradeforge_api.worker.WorkerSettings`) drains the queue and does the CPU-bound
work. Progress flows back the other way over a Redis **pub/sub** channel: the worker
publishes, the WebSocket endpoint subscribes. They are two processes with no shared memory,
so a percentage cannot simply be handed across — it has to travel through Redis like the job
itself did.
"""

import uuid
from typing import Protocol

from arq.connections import RedisSettings

from tradeforge_api.config import Settings

# The job name. The worker registers a coroutine under exactly this string (see `worker.py`),
# and the router enqueues by it — a mismatch would enqueue jobs no worker ever claims.
RUN_BACKTEST = "run_backtest"


class JobQueue(Protocol):
    """The one capability the API needs from the queue: enqueue a job by name. Depending on
    this rather than on `ArqRedis` keeps the handlers testable — the real pool and the test
    fakes both satisfy it structurally, so a test can inject a recorder in the pool's place."""

    async def enqueue_job(self, function: str, *args: object) -> object: ...


def redis_settings(settings: Settings) -> RedisSettings:
    """arq's own connection settings, built from the same host/port the rest of the app uses."""
    return RedisSettings(host=settings.redis_host, port=settings.redis_port)


def progress_channel(backtest_id: uuid.UUID) -> str:
    """The pub/sub channel one backtest's progress flows through. Per-run, so a subscriber
    hears only the run it asked about."""
    return f"backtest:progress:{backtest_id}"

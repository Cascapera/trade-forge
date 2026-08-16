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

from tradeforge_api.config import RedisConfig

# The job name. The worker registers a coroutine under exactly this string (see `worker.py`),
# and the router enqueues by it — a mismatch would enqueue jobs no worker ever claims.
RUN_BACKTEST = "run_backtest"

RUN_WALK_FORWARD = "run_walk_forward"
"""The orchestrating job: one per walk-forward, not one per run.

⚠️ **A walk-forward's training runs are deliberately never enqueued.** They are written as
`queued` rows and executed by this job, in order, inside a single worker slot. Queueing them
too would mean two claimants for the same row, and the alternative — this job enqueuing them
and *waiting* — deadlocks outright on one worker, because `execute_backtest` is CPU-bound and
synchronous, so the waiter holds the only slot the runs it waits for would need. See
`worker.process_walk_forward`.
"""


class JobQueue(Protocol):
    """The one capability the API needs from the queue: enqueue a job by name. Depending on
    this rather than on `ArqRedis` keeps the handlers testable — the real pool and the test
    fakes both satisfy it structurally, so a test can inject a recorder in the pool's place."""

    async def enqueue_job(self, function: str, *args: object, **options: object) -> object: ...

    """⚠️ `**options` is not decoration: arq's own `enqueue_job` takes keyword options, and
    `_job_id` is the one that matters here. A job id derived from the row it runs makes an
    enqueue **idempotent**, so a request that dies partway through queueing N jobs can be
    retried rather than reconciled by hand. A protocol that omitted them would be narrower than
    the thing it describes, and the narrowing would only ever be discovered by a caller that
    needed one."""


def redis_settings(settings: RedisConfig) -> RedisSettings:
    """arq's own connection settings, built from the same host/port the rest of the app uses.

    Takes `RedisConfig`, not the full `Settings`, so the worker can build these at import time
    without a Postgres password on hand — a full `Settings` still satisfies it by inheritance.
    """
    return RedisSettings(host=settings.redis_host, port=settings.redis_port)


def progress_channel(backtest_id: uuid.UUID) -> str:
    """The pub/sub channel one backtest's progress flows through. Per-run, so a subscriber
    hears only the run it asked about."""
    return f"backtest:progress:{backtest_id}"

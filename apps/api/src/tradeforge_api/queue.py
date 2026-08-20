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


SYNC_SYMBOLS = "sync_symbols"
"""Photograph the broker's symbol catalogue into `broker_symbols`.

⚠️ **Runs on `COLLECT_QUEUE`, which no Linux worker drains.** The job needs MetaTrader, and
MetaTrader exists only on the Windows host (ADR-02, and the wheel does not install on Linux).
Enqueuing it on the default queue would hand it to a container that can never execute it — a
job that sits `queued` for ever with nothing raised, which is the failure this constant's
placement is here to prevent.
"""

PROBE_HISTORY = "probe_history"
"""Measure how much history one (symbol, timeframe) really has.

⚠️ Also on `COLLECT_QUEUE`, and slow enough that the queue is the point rather than a nicety:
measured at 207 seconds for a cold H4, because the terminal downloads the history while it
answers. A handler that waited would hold a request open for three and a half minutes.
"""

COLLECT_RANGE = "collect_range"
"""Download one `collections` row's range and catalogue what lands on disk.

⚠️ **Takes the row's id and nothing else.** Symbol, timeframe, window and asset class are all
already on the row this API wrote before answering 202 — the same row `GET /collections/{id}`
reports and the screen renders. A payload repeating them would be a second copy free to
disagree with the one on screen, and the disagreement would be silent: the screen would show
the window it stored while the agent downloaded the window it was handed.
"""

COLLECT_JOBS = (SYNC_SYMBOLS, PROBE_HISTORY, COLLECT_RANGE)
"""Every job that only the Windows host can run, as one list.

⚠️ **Named as a set rather than left implicit, so the contract test can be exact in both
directions.** Checking "is `sync_symbols` registered?" passes forever while a second job is
added and forgotten; comparing this tuple against what the agent registers catches an addition
on either side. The failure it prevents is silent by construction — the API returns 202, Redis
accepts the job, and it waits in a queue nobody is watching.
"""

COLLECT_QUEUE = "collect"
"""The queue only the host agent (`tradeforge-collector agent`) drains.

Two queues rather than one, for two independent reasons and either would be enough:

* **Capability.** A worker in a container physically cannot run a job that calls MetaTrader.
  Segregation is what makes "who can claim this" a property of the queue instead of a hope.
* **Latency.** Syncing a symbol list is a sub-second job. Sharing a queue with a walk-forward
  would put it behind twenty minutes of CPU-bound backtesting, on a click a human is watching.
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

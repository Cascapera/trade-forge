"""How a sweep's jobs go on the queue (`batching.enqueue_runs`)."""

import asyncio
import datetime as dt
import uuid
from typing import Any

from tradeforge_api.batching import SWEEP_JOB_EXPIRES, enqueue_runs
from tradeforge_api.queue import RUN_BACKTEST, RUN_BACKTEST_BATCH


class Queue:
    def __init__(self) -> None:
        self.jobs: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    async def enqueue_job(self, function: str, *args: Any, **options: Any) -> None:
        self.jobs.append((function, args, options))


def test_a_sweep_s_jobs_wait_far_longer_than_arq_s_one_day() -> None:
    """13/10: 2.3 million waiting jobs of a week-long sweep expired together 24 h after launch."""
    alone, first, second = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    queue = Queue()

    asyncio.run(enqueue_runs(queue, [[alone], [first, second]]))

    assert queue.jobs == [
        (RUN_BACKTEST, (str(alone),), {"_job_id": str(alone), "_expires": SWEEP_JOB_EXPIRES}),
        (
            RUN_BACKTEST_BATCH,
            ([str(first), str(second)],),
            {"_job_id": f"batch-{first}", "_expires": SWEEP_JOB_EXPIRES},
        ),
    ]
    assert dt.timedelta(days=30) <= SWEEP_JOB_EXPIRES

"""Which runs of a launch go to a worker together, and how they are queued (ADR-0029).

A sweep's runs over one market — one symbol, one chart, one window, one reading of the market
(`shared_reading`: the timeframe above and the broker's clock) — run as a batch: one job, the
market read once per bar for all of them (`runner.execute_batch`). Everything else runs as it
always has, one job per run: a run whose document reads no shared market, and a run waiting for
a download (its wait is `run_backtest`'s, per run).

⚠️ **`BATCH_SIZE` is a trade, not a tuning knob.** Larger batches share the reading among more
runs (the gain grows towards the setup's own cost), but a batch is also the unit that fails
together and lands together: a worker that dies loses every run of its batch, and a batch's
results arrive at its end. Twenty-four M5 runs are about a minute and a half on this machine.
"""

import datetime as dt
import uuid
from collections.abc import Mapping, Sequence
from typing import Any

from tradeforge_api.queue import RUN_BACKTEST, RUN_BACKTEST_BATCH, JobQueue
from tradeforge_api.warm_window import WarmUp, warmup_for
from tradeforge_collector import step
from tradeforge_engine.setup_factory import shared_reading

BATCH_SIZE = 24

_SMALLER: dict[str, int] = {"M1": 6}
"""Charts whose runs are so long that a batch of `BATCH_SIZE` would near the batch's timeout
(`worker.BATCH_TIMEOUT_SECONDS`) on the Xeon's slower cores: M1 has five times M5's bars."""


def batch_size(timeframe: str) -> int:
    return _SMALLER.get(timeframe, BATCH_SIZE)


BatchKey = tuple[str, str, tuple[dt.timedelta | None, dt.timedelta], WarmUp]
"""(symbol, chart, reading, warm-up) — every run of one launch already shares its window. The
warm-up because a batch feeds one stream of bars to all its runs (ADR-0030, `warm_window`)."""


def batch_key(document: Mapping[str, Any], symbol: str, timeframe: str) -> BatchKey | None:
    """The batch a run of this document on this market belongs to, or `None` to run alone."""
    setup = document.get("setup")
    if not isinstance(setup, Mapping):
        return None
    reading = shared_reading(setup, timeframe=step(timeframe))
    return None if reading is None else (symbol, timeframe, reading, warmup_for(document))


class Batcher:
    """Collects a launch's runs in launch order and cuts them into jobs."""

    def __init__(self, size: int | None = None, *, enabled: bool = True) -> None:
        self._size = size
        self._enabled = enabled
        self._groups: dict[BatchKey, list[uuid.UUID]] = {}
        self._alone: list[uuid.UUID] = []

    def add(self, run_id: uuid.UUID, key: BatchKey | None) -> None:
        if key is None or not self._enabled:
            self._alone.append(run_id)
        else:
            self._groups.setdefault(key, []).append(run_id)

    def jobs(self) -> list[list[uuid.UUID]]:
        """Each job's runs: batches of up to `size` per key in the order the keys were met, then
        the runs that go alone, one each. A batch of one is a run alone."""
        jobs = []
        for (_symbol, timeframe, _reading, _warmup), runs in self._groups.items():
            size = self._size or batch_size(timeframe)
            jobs += [runs[start : start + size] for start in range(0, len(runs), size)]
        return jobs + [[run_id] for run_id in self._alone]


def runs_in(jobs: Sequence[Sequence[uuid.UUID]]) -> int:
    return sum(len(job) for job in jobs)


async def enqueue_runs(queue: JobQueue, jobs: Sequence[Sequence[uuid.UUID | str]]) -> None:
    """Queue each job: a run alone as `run_backtest`, a batch as `run_backtest_batch`.

    ⚠️ **Job ids derived from the runs**, as they always were: re-sending a launch's jobs after a
    crash queues nothing twice. A batch is named by its first run, which no other batch holds.
    """
    for job in jobs:
        ids = [str(run_id) for run_id in job]
        if len(ids) == 1:
            await queue.enqueue_job(RUN_BACKTEST, ids[0], _job_id=ids[0])
        else:
            await queue.enqueue_job(RUN_BACKTEST_BATCH, ids, _job_id=f"batch-{ids[0]}")


__all__ = [
    "BATCH_SIZE",
    "BatchKey",
    "Batcher",
    "batch_key",
    "batch_size",
    "enqueue_runs",
    "runs_in",
]

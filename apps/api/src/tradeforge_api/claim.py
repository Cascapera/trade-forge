"""A worker that claims its next job in one round trip to Redis, however many are taken (06/10).

arq keeps a job in the queue's sorted set while it runs; only an `in-progress` key says it is
taken. To find a free job a worker reads the head of the queue and tries each id in turn — WATCH,
EXISTS, ZSCORE, and the pipeline's reset — three or four round trips per job some other worker
already holds. Next to Redis that is nothing. From a machine across the internet it is the whole
gap between two batches: 32 workers busy, ~4 trips each, 117 ms a trip (AWS, Virginia) ≈ 15 s
idle after every 75 s batch; at 210 ms (Contabo) ≈ 30 s after every 205 s one.

`OneTripWorker` hands the ids arq read to a Lua script that walks them inside Redis, skips the
taken ones and takes the first free one — the same `in-progress` key, the same lifetime, the
same "not before its score" rule as arq. Redis runs a script whole, with nothing in between, so
no WATCH is needed for two workers not to take one job; and because the key is arq's own, a
worker still started with the `arq` command sees this one's claims, and this one sees its.

⚠️ **It replaces one method of arq's `Worker`** (`start_jobs`, arq 0.28) and leans on its
bookkeeping (`sem`, `job_counter`, `tasks`, `run_job`). arq is pinned in `uv.lock`; the
integration test runs the real worker on a real Redis and fails if an upgrade moves any of it.
"""

from __future__ import annotations

from arq.constants import in_progress_key_prefix
from arq.utils import timestamp_ms
from arq.worker import Worker
from redis.commands.core import AsyncScript

# KEYS[1] the queue · ARGV[1] now (ms) · ARGV[2] the in-progress key's lifetime (ms) ·
# ARGV[3] the in-progress key prefix · ARGV[4…] the candidate ids, in queue order.
# Returns {job_id, score} for the job it took, or nil when every candidate was taken or gone.
CLAIM_SCRIPT = """
local now = tonumber(ARGV[1])
for i = 4, #ARGV do
  local job_id = ARGV[i]
  local key = ARGV[3] .. job_id
  if redis.call('EXISTS', key) == 0 then
    local score = redis.call('ZSCORE', KEYS[1], job_id)
    if score and tonumber(score) <= now then
      redis.call('PSETEX', key, ARGV[2], '1')
      return {job_id, score}
    end
  end
end
return nil
"""


class OneTripWorker(Worker):
    """arq's `Worker`, taking each job with one script call instead of a try per taken job."""

    # Registered on the pool at the first claim: arq makes the pool in `main`, after `__init__`.
    _claim: AsyncScript | None = None

    async def claim(self, job_ids: list[str]) -> tuple[str, int] | None:
        """Take the first job of `job_ids` nobody holds and whose time has come, or None."""
        if not job_ids:
            return None
        if self._claim is None:
            self._claim = self.pool.register_script(CLAIM_SCRIPT)
        taken = await self._claim(
            keys=[self.queue_name],
            args=[
                timestamp_ms(),
                int(self.in_progress_timeout_s * 1000),
                in_progress_key_prefix,
                *job_ids,
            ],
        )
        if not taken:
            return None
        job_id, score = taken
        return (job_id.decode() if isinstance(job_id, bytes) else str(job_id)), int(float(score))

    async def start_jobs(self, job_ids: list[bytes]) -> None:
        """Start as many of `job_ids` as there are free slots, each claimed in one round trip."""
        remaining = [job_id.decode() for job_id in job_ids]
        while remaining:
            await self.sem.acquire()
            if self.job_counter >= self.max_jobs:
                self.sem.release()
                return
            self.job_counter = self.job_counter + 1
            claimed = await self.claim(remaining)
            if claimed is None:
                self.job_counter = self.job_counter - 1
                self.sem.release()
                return
            job_id, score = claimed
            # Ids before the one taken were all held or gone when the script looked.
            remaining = remaining[remaining.index(job_id) + 1 :]
            task = self.loop.create_task(self.run_job(job_id, score))
            task.add_done_callback(lambda _: self._release_sem_dec_counter_on_complete())
            self.tasks[job_id] = task


__all__ = ["CLAIM_SCRIPT", "OneTripWorker"]

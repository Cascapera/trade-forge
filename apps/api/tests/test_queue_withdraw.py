"""Taking waiting jobs out of the queue, and counting the ones a worker holds (02/10)."""

import asyncio
from collections.abc import AsyncIterator

from tradeforge_api.queue import WITHDRAW_BLOCK, jobs_in_progress, withdraw_jobs


class FakeRedis:
    """The few Redis commands a pause and a resume speak, over plain sets."""

    def __init__(self, queued: set[str], in_progress: frozenset[str] = frozenset()) -> None:
        self.queued = set(queued)
        self.keys = {f"arq:job:{job}" for job in queued} | {
            f"arq:in-progress:{job}" for job in in_progress
        }
        self.calls = 0

    async def zrem(self, name: str, *values: str) -> int:
        assert name == "arq:queue"
        self.calls += 1
        gone = self.queued & set(values)
        self.queued -= gone
        return len(gone)

    async def delete(self, *names: str) -> int:
        gone = self.keys & set(names)
        self.keys -= gone
        return len(gone)

    async def scan_iter(self, match: str, count: int) -> AsyncIterator[str]:
        prefix = match.rstrip("*")
        for key in sorted(self.keys):
            if key.startswith(prefix):
                yield key


def test_only_the_jobs_still_waiting_are_withdrawn_and_counted() -> None:
    redis = FakeRedis({"a", "b", "c"})

    withdrawn = asyncio.run(withdraw_jobs(redis, ["a", "b", "x"]))

    assert withdrawn == 2
    assert redis.queued == {"c"}
    assert redis.keys == {"arq:job:c"}


def test_a_long_list_goes_in_blocks() -> None:
    ids = [str(n) for n in range(WITHDRAW_BLOCK * 2 + 1)]
    redis = FakeRedis(set(ids))

    assert asyncio.run(withdraw_jobs(redis, ids)) == len(ids)
    assert redis.calls == 3


def test_a_queue_that_is_not_redis_withdraws_nothing_and_cannot_count() -> None:
    assert asyncio.run(withdraw_jobs(object(), ["a"])) == 0
    assert asyncio.run(jobs_in_progress(object())) is None


def test_the_jobs_a_worker_holds_are_counted() -> None:
    assert asyncio.run(jobs_in_progress(FakeRedis({"a"}, frozenset({"b", "c"})))) == 2
    assert asyncio.run(jobs_in_progress(FakeRedis({"a"}))) == 0

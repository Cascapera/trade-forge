"""The heavy pages' snapshots, computed off the request (05/10).

His report: the PC slowed, and the API held 14 GB. An open page of a sweep of 563 thousand runs
asked for its summary every few seconds — 75 s and several GB each — and the best-by-market map
ranked every run of every sweep on each opening, 30 s. Both now read the last snapshot kept, and
this module computes the next ones:

* **a sweep's summary** (`routers.sweeps.refresh_summary`), every `SUMMARY_EVERY`, for each large
  sweep whose run counts moved since its last one (`routers.sweeps.SMALL_SWEEP`; a small one is
  computed on the request as before);
* **the best-by-market maps** (`routers.best.compute_best_map`), every `BEST_EVERY`, when a run
  has finished since the last ones were computed — only the maps opened in the last `BEST_READ`:
  ten metrics, each with and without the floor, are twenty queries of half a minute (05/10), and a
  refresh of all of them would keep Postgres busy the whole time a sweep runs.

⚠️ **In a process of its own, one at a time, that ends after each computation.** Python keeps
the memory a computation of several GB asked for; done in the API, every page after it would run
on a 14 GB process. A child process returns it all when it exits.
"""

import asyncio
import datetime as dt
import logging
import multiprocessing
import uuid
from collections.abc import Callable
from concurrent.futures import Executor, ProcessPoolExecutor
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from tradeforge_api.best import BestMetric
from tradeforge_api.schemas import BestMapOut
from tradeforge_api.snapshot_store import SnapshotStore
from tradeforge_db.models import Backtest, BacktestStatus, Sweep
from tradeforge_db.session import create_db_engine, create_session_factory

logger = logging.getLogger(__name__)

SUMMARY_EVERY = dt.timedelta(minutes=2)
BEST_EVERY = dt.timedelta(minutes=30)
BEST_READ = dt.timedelta(days=1)


def child_pool() -> Executor:
    """One process at a time, a fresh one per computation (`max_tasks_per_child=1`)."""
    return ProcessPoolExecutor(
        max_workers=1, mp_context=multiprocessing.get_context("spawn"), max_tasks_per_child=1
    )


# --------------------------------------------------------------------------- #
# What runs in the child — each opens its own connection from the environment.
# --------------------------------------------------------------------------- #


def summary_in_child(sweep_id: str) -> None:
    """Compute one sweep's summary and keep it on its row."""
    from tradeforge_api.routers.sweeps import refresh_summary  # noqa: PLC0415 — child only

    engine = create_db_engine()
    try:
        with create_session_factory(engine)() as session:
            sweep = session.get(Sweep, uuid.UUID(sweep_id))
            if sweep is not None:
                refresh_summary(session, sweep)
    finally:
        engine.dispose()


def best_in_child(metric: str, every_run: bool) -> str:
    """One best-by-market map, as JSON for the parent to keep."""
    from tradeforge_api.routers.best import compute_best_map  # noqa: PLC0415 — child only

    engine = create_db_engine()
    try:
        with create_session_factory(engine)() as session:
            return compute_best_map(
                session, BestMetric(metric), every_run=every_run
            ).model_dump_json()
    finally:
        engine.dispose()


# --------------------------------------------------------------------------- #
# What the parent decides.
# --------------------------------------------------------------------------- #


def in_child(fn: Callable[..., Any], *args: Any) -> Any:  # noqa: ANN401 — the call's result
    """Run one computation in a process of its own and wait for it — for a request that has no
    snapshot to serve yet (a large sweep opened before the loop reached it)."""
    with child_pool() as pool:
        return pool.submit(fn, *args).result()


def stale_summaries(session: Session) -> list[uuid.UUID]:
    """The large sweeps whose kept summary was computed at other run counts, or never.

    One grouped count over every run, read whole (about a second on 3.3 million runs, 05/10);
    a combination of sweeps is left to its own page, which computes it once when it settles.
    """
    # Imported here: the router imports this module's neighbours, and the counts' shape is its.
    from tradeforge_api.routers.sweeps import SMALL_SWEEP  # noqa: PLC0415

    counted: dict[uuid.UUID, dict[BacktestStatus, int]] = {}
    for sweep_id, state, many in session.execute(
        select(Backtest.sweep_id, Backtest.status, func.count())
        .where(Backtest.sweep_id.is_not(None))
        .group_by(Backtest.sweep_id, Backtest.status)
    ):
        counted.setdefault(sweep_id, dict.fromkeys(BacktestStatus, 0))[state] = many
    large = {one: counts for one, counts in counted.items() if sum(counts.values()) > SMALL_SWEEP}
    if not large:
        return []
    stale = []
    for sweep in session.scalars(select(Sweep).where(Sweep.id.in_(list(large)))):
        if sweep.combines:
            continue
        counts = large[sweep.id]
        now = {
            "total": sum(counts.values()),
            "done": counts[BacktestStatus.DONE],
            "running": counts[BacktestStatus.RUNNING],
            "queued": counts[BacktestStatus.QUEUED],
            "failed": counts[BacktestStatus.FAILED],
        }
        kept = sweep.summary or {}
        if kept.get("counts") != now:
            stale.append(sweep.id)
    return stale


def runs_fingerprint(session: Session) -> str:
    """What the maps were computed from: how many runs are done, and the last one to finish."""
    done, last = session.execute(
        select(func.count(), func.max(Backtest.finished_at)).where(
            Backtest.status == BacktestStatus.DONE
        )
    ).one()
    return f"{done}:{None if last is None else last.isoformat()}"


def refresh_summaries(
    session_factory: Callable[[], Session], run: Callable[..., Any]
) -> list[uuid.UUID]:
    """Compute again every stale large sweep's summary; the sweeps it computed."""
    with session_factory() as session:
        stale = stale_summaries(session)
    for sweep_id in stale:
        run(summary_in_child, str(sweep_id))
    return stale


def refresh_best(
    session_factory: Callable[[], Session], store: SnapshotStore, run: Callable[..., Any]
) -> bool:
    """Compute again the maps opened in the last `BEST_READ` if a run finished since the last
    ones; whether it did. A map nobody opened is computed on its first opening."""
    with session_factory() as session:
        fingerprint = runs_fingerprint(session)
    if fingerprint == store.best_fingerprint():
        return False
    since = dt.datetime.now(tz=dt.UTC) - BEST_READ
    for metric in BestMetric:
        for every_run in (False, True):
            read = store.read_since(metric, every_run=every_run)
            if read is None or read < since:
                continue
            made = run(best_in_child, metric.value, every_run)
            store.keep_best(BestMapOut.model_validate_json(made))
    store.keep_best_fingerprint(fingerprint)
    return True


async def refresh_forever(
    session_factory: Callable[[], Session], store: SnapshotStore, pool: Executor
) -> None:
    """The API's background loop: summaries every `SUMMARY_EVERY`, maps every `BEST_EVERY`.

    A failure is logged and the loop goes on — a snapshot a few minutes old is the worst it costs.
    """

    def run(fn: Callable[..., Any], *args: Any) -> Any:  # noqa: ANN401 — a pickled call's result
        return pool.submit(fn, *args).result()

    best_due = dt.datetime.now(tz=dt.UTC)
    while True:
        await asyncio.sleep(SUMMARY_EVERY.total_seconds())
        try:
            await asyncio.to_thread(refresh_summaries, session_factory, run)
            if dt.datetime.now(tz=dt.UTC) >= best_due:
                await asyncio.to_thread(refresh_best, session_factory, store, run)
                best_due = dt.datetime.now(tz=dt.UTC) + BEST_EVERY
        except Exception:
            logger.exception("refreshing the snapshots failed; the last ones are served")


__all__ = [
    "BEST_EVERY",
    "SUMMARY_EVERY",
    "best_in_child",
    "child_pool",
    "in_child",
    "refresh_best",
    "refresh_forever",
    "refresh_summaries",
    "runs_fingerprint",
    "stale_summaries",
    "summary_in_child",
]

"""Recording a collection request and the states it passes through.

Written from both sides of the ADR-02 boundary, unlike `symbol_history` next door: the API
creates the row (it is what `POST /collections` has to hand back an id for) and the host agent
moves it along. Two writers, so every transition below states what it is allowed to assume
about the row it is changing.
"""

import datetime as dt
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from tradeforge_db.models import BacktestStatus, Collection
from tradeforge_engine.domain import AssetClass

__all__ = [
    "create_collection",
    "finish_collection",
    "read_collection",
    "recent_collections",
    "record_progress",
    "start_collection",
]


def create_collection(  # noqa: PLR0913 — keyword-only; these are the columns of the request
    session: Session,
    *,
    symbol: str,
    timeframe: str,
    date_from: dt.datetime,
    date_to: dt.datetime,
    asset_class: AssetClass | None,
    years_total: int,
) -> Collection:
    """Write the request down and return it, so the API has an id to answer 202 with.

    ⚠️ `years_total` is computed by the **caller**, from `collect.year_slices`, rather than
    left at zero for the agent to fill in. The screen shows "0 of 5 years" the instant the
    request lands; leaving it zero would show "0 of 0", which reads as finished.
    """
    collection = Collection(
        symbol=symbol,
        timeframe=timeframe,
        date_from=date_from,
        date_to=date_to,
        asset_class=asset_class,
        status=BacktestStatus.QUEUED,
        years_done=0,
        years_total=years_total,
    )
    session.add(collection)
    session.flush()
    return collection


def start_collection(session: Session, collection_id: uuid.UUID, *, at: dt.datetime) -> None:
    """Mark the job as picked up. Silently does nothing if the row is gone.

    ⚠️ Silent on a missing row **here and only here**, because this transition is the one the
    agent performs before it has done any work. A row deleted between request and pickup is a
    request somebody cancelled, and crashing the worker over it would take the next queued
    collection down with it.
    """
    collection = session.get(Collection, collection_id)
    if collection is None:
        return
    collection.status = BacktestStatus.RUNNING
    collection.started_at = at


def record_progress(session: Session, collection_id: uuid.UUID, *, years_done: int) -> None:
    """Say how many years are on disk so far.

    Called once per slice, which is the only granularity the work actually has: a year is
    downloaded, written and either found or found empty as a single step.
    """
    collection = session.get(Collection, collection_id)
    if collection is None:
        return
    collection.years_done = years_done


def finish_collection(  # noqa: PLR0913 — keyword-only; the outcome has this many parts
    session: Session,
    collection_id: uuid.UUID,
    *,
    at: dt.datetime,
    candles: int | None = None,
    gaps: int | None = None,
    error: str | None = None,
) -> None:
    """Close the row out, as done or as failed.

    ⚠️ **The status is derived from `error`, not passed in beside it.** A caller free to say
    "failed" while leaving the reason `None` produces a row that reports a failure nobody can
    read, and one free to say "done" while setting an error produces the opposite. Deriving
    makes the pair impossible to contradict.
    """
    collection = session.get(Collection, collection_id)
    if collection is None:
        return
    collection.status = BacktestStatus.FAILED if error is not None else BacktestStatus.DONE
    collection.error = error
    collection.candles = candles
    collection.gaps = gaps
    collection.finished_at = at


def read_collection(session: Session, collection_id: uuid.UUID) -> Collection | None:
    """One request and its state, or `None` if no such request was ever made."""
    return session.get(Collection, collection_id)


def recent_collections(session: Session, *, limit: int = 20) -> list[Collection]:
    """The newest requests first — what the screen lists under the form.

    Ordered by `requested_at` and then by `id`, because the primary key is a UUID and carries
    no order of its own: two requests landing inside the same clock tick would otherwise come
    back in whatever order the scan happened to produce, and the list would reshuffle between
    polls while nothing changed.
    """
    statement = (
        select(Collection)
        .order_by(Collection.requested_at.desc(), Collection.id.desc())
        .limit(limit)
    )
    return list(session.scalars(statement))

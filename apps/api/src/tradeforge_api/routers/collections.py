"""`/collections` — asking the host to go and fetch a symbol's history, and watching it happen.

The API never speaks to MetaTrader (ADR-02). What it does here is write down what was asked
for, drop the row's id on the queue only the host agent drains, and answer 202. Everything a
caller learns afterwards comes from the row.

## The one thing this endpoint decides for itself

Whether the asset class is knowable. `instruments.asset_class` is NOT NULL with five legal
values, so a collection that cannot supply one cannot be catalogued — and the symbol's tree
path is the only evidence available. The snapshot already stores that path, so the check
happens *here*, before the request is accepted, rather than three minutes later on the host as
a job that failed.

⚠️ **The difference is not politeness, it is whether the answer is actionable.** A person who
gets `409: I cannot tell what CFDs\\XAUUSD is` while looking at the form fills in a field. The
same person, told nothing until a background job fails, has already navigated away.
"""

import uuid

from fastapi import APIRouter, HTTPException, status

from tradeforge_api.deps import QueueDep, SessionDep
from tradeforge_api.queue import COLLECT_QUEUE, COLLECT_RANGE
from tradeforge_api.schemas import CollectionOut, CreateCollectionRequest
from tradeforge_collector.classify import asset_class_from_path

# ⚠️ Importing a pure function out of `apps/collector`, which `apps/api/pyproject.toml` already
# depends on for the Parquet reader. Safe on Linux because `classify` holds no MetaTrader import
# — that is why it is its own module rather than living beside `MT5Source`, whose first line of
# docstring says it is never imported here.
from tradeforge_collector.collect import year_slices
from tradeforge_db.broker_symbols import symbol_path
from tradeforge_db.collections import create_collection, read_collection, recent_collections

router = APIRouter(tags=["collections"])


@router.post("/collections", response_model=CollectionOut, status_code=status.HTTP_202_ACCEPTED)
async def create(
    session: SessionDep, queue: QueueDep, request: CreateCollectionRequest
) -> CollectionOut:
    """Accept a collection and hand back the row to watch it with.

    ⚠️ **202 with a body, where the other queued endpoints answer with a job name.** `EnqueuedOut`
    carries no id because a sync's only observable result is the data it replaces, so there is
    nothing to poll. A collection is the opposite: it takes minutes, it can fail with a reason
    worth reading, and the id below is the only way anybody finds out either.

    ⚠️ **The row is written before the job is queued, and the order is load-bearing.** Queue
    first and the agent can pick up an id that does not exist yet; a job that raced ahead of its
    own row would log "collection no longer exists" and vanish, and nothing would ever explain
    why the screen showed nothing.
    """
    # `or ""` collapses two situations the caller cannot act on differently: the symbol is not
    # in the snapshot at all, or it is there with no path. Neither one decides a class.
    from_path = asset_class_from_path(symbol_path(session, request.symbol) or "")
    if request.asset_class is None and from_path is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"cannot tell what kind of instrument {request.symbol} is from where the broker "
                f"files it, so it cannot be catalogued — say which asset class it is"
            ),
        )

    collection = create_collection(
        session,
        symbol=request.symbol,
        timeframe=request.timeframe,
        date_from=request.date_from,
        date_to=request.date_to,
        # Stored only when a person supplied it, so the row records who decided. NULL means the
        # path did — which is the difference between provenance and a duplicated derivation.
        asset_class=request.asset_class,
        # Counted here rather than left to the agent: the screen renders "0 of 5 years" the
        # instant the request lands, and "0 of 0" reads as finished.
        years_total=len(year_slices(request.date_from, request.date_to)),
    )
    session.commit()

    await queue.enqueue_job(COLLECT_RANGE, str(collection.id), _queue_name=COLLECT_QUEUE)
    return CollectionOut.model_validate(collection)


@router.get("/collections", response_model=list[CollectionOut])
def index(session: SessionDep) -> list[CollectionOut]:
    """The most recent requests, newest first — what the screen lists under the form."""
    return [CollectionOut.model_validate(row) for row in recent_collections(session)]


@router.get("/collections/{collection_id}", response_model=CollectionOut)
def show(session: SessionDep, collection_id: uuid.UUID) -> CollectionOut:
    """One request and its current state. This is what the screen polls while it runs."""
    found = read_collection(session, collection_id)
    if found is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"no collection {collection_id}"
        )
    return CollectionOut.model_validate(found)

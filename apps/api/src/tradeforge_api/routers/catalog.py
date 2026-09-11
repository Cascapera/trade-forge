"""`/catalog` — the shelf: strategies with a label a person wrote and the sweep they are meant
to be asked.

**Why this exists beside `/strategies`.** That endpoint answers "what documents does this
database hold", and it is the right answer to that question. It is not a catalogue: the names it
returns are generated columns projected out of the documents, so they are whatever the builder
stamped, and this project's own database is eleven rows of `MME9-20260910-172055`. A shelf needs
labels, and labels are written by people.

The second half is the **grid**. Until this table there was nowhere to keep one: a grid was
typed into the study screen and thrown away at launch, surviving only inside `studies.grid` as a
record of what a study *did* search. `9.1 over every average period` was therefore not a thing
anybody could save, only a thing they could retype.

⚠️ **The grid is checked for reach, not for meaning, and the difference is the timeframe.**
`expand` answers whether every axis names a path this document has and whether the product is
inside the cap — facts about the document alone, true whenever the entry is read. Whether a
*point* can run also depends on the timeframe it runs at (`runner.timeframe_refusal`), and an
entry does not carry one: the same shelf entry is meant to be swept across several. So the
semantic verdict belongs to the launch, where a timeframe exists, and `/studies/preview` already
gives it there.
"""

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from tradeforge_api.deps import SessionDep
from tradeforge_api.grid import GridError, expand, size_of
from tradeforge_api.routers.strategies import setup_of
from tradeforge_api.schemas import (
    CatalogEntryOut,
    CatalogPage,
    CreateCatalogEntry,
)
from tradeforge_db.models import CatalogEntry, Strategy

router = APIRouter(tags=["catalog"])

_Responses = dict[int | str, dict[str, Any]]
_NOT_FOUND: _Responses = {status.HTTP_404_NOT_FOUND: {"description": "entry not found"}}
_CONFLICT: _Responses = {status.HTTP_409_CONFLICT: {"description": "this name is taken"}}
_BAD_BODY: _Responses = {status.HTTP_400_BAD_REQUEST: {"description": "malformed request body"}}


def _out(entry: CatalogEntry, strategy: Strategy) -> CatalogEntryOut:
    return CatalogEntryOut(
        id=entry.id,
        name=entry.name,
        description=entry.description,
        strategy_id=entry.strategy_id,
        strategy_name=strategy.name,
        strategy_version=strategy.version,
        setup=setup_of(strategy.definition),
        grid=dict(entry.grid),
        # Derived, never stored. A count kept beside the grid is a second place for one fact,
        # and the day an axis is edited without it is the day the shelf lies about the size of
        # an afternoon. `size_of` is the same arithmetic the study's own preview reports.
        #
        # ⚠️ No special case for the empty grid, and the absence was measured: `size_of({})` is
        # the empty product, which is 1 — an entry with nothing to vary is one backtest. An
        # `if entry.grid else 1` here produces the identical number and reads as a claim that
        # empty needs handling, which sends the next reader looking for a case that is not there.
        points=size_of(entry.grid),
        created_at=entry.created_at,
    )


@router.post(
    "/catalog",
    response_model=CatalogEntryOut,
    status_code=status.HTTP_201_CREATED,
    responses={**_NOT_FOUND, **_CONFLICT, **_BAD_BODY},
)
def create_entry(request: CreateCatalogEntry, session: SessionDep) -> CatalogEntryOut:
    """Save a shelf entry, refusing a grid that does not fit the strategy it names.

    ⚠️ **The grid is expanded here and the result thrown away**, which is the point: an axis
    naming a path the document does not have is not a launch that fails later, it is an entry
    that was never a coherent thing to save. Finding that out at save time costs one expansion
    of a grid somebody is looking at; finding it out at launch costs a refusal against a shelf
    entry they had stopped thinking about.

    The 409 is the database's, not a lookup's. Checking whether a name is taken and then
    inserting is two statements with a gap between them, and a catalogue is exactly the kind of
    thing two browser tabs write to at once.
    """
    strategy = session.get(Strategy, request.strategy_id)
    if strategy is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="strategy not found")

    if request.grid:
        try:
            expand(strategy.definition, request.grid)
        except GridError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
            ) from exc

    entry = CatalogEntry(
        name=request.name,
        description=request.description,
        strategy_id=strategy.id,
        grid=dict(request.grid),
    )
    session.add(entry)
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"a catalogue entry named {request.name!r} already exists",
        ) from exc
    session.refresh(entry)
    return _out(entry, strategy)


@router.get("/catalog", response_model=CatalogPage)
def list_entries(
    session: SessionDep,
    *,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
    offset: Annotated[int, Query(ge=0, le=10_000)] = 0,
) -> CatalogPage:
    """The shelf, newest first.

    One statement, joined rather than one lookup per row: a catalogue is read whole and every
    entry needs its strategy's setup and version, so the N+1 is not hypothetical here — it is
    the default shape of this endpoint if the join is left out.

    ⚠️ A ceiling on `limit`, like every other listing here. Without one a single request is as
    large as the caller likes, and a catalogue is the endpoint most likely to be polled.
    """
    rows = session.execute(
        select(CatalogEntry, Strategy)
        .join(Strategy, Strategy.id == CatalogEntry.strategy_id)
        .order_by(CatalogEntry.created_at.desc())
        .limit(limit)
        .offset(offset)
    ).all()
    total = session.scalar(select(func.count()).select_from(CatalogEntry)) or 0
    return CatalogPage(total=total, items=[_out(entry, strategy) for entry, strategy in rows])


@router.get("/catalog/{entry_id}", response_model=CatalogEntryOut, responses=_NOT_FOUND)
def get_entry(entry_id: uuid.UUID, session: SessionDep) -> CatalogEntryOut:
    row = session.execute(
        select(CatalogEntry, Strategy)
        .join(Strategy, Strategy.id == CatalogEntry.strategy_id)
        .where(CatalogEntry.id == entry_id)
    ).first()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="entry not found")
    entry, strategy = row
    return _out(entry, strategy)


@router.delete("/catalog/{entry_id}", status_code=status.HTTP_204_NO_CONTENT, responses=_NOT_FOUND)
def delete_entry(entry_id: uuid.UUID, session: SessionDep) -> None:
    """Take a label off the shelf.

    ⚠️ **The strategy is untouched, and that is not an omission.** The document may have runs
    pointing at it, and a run answers "what did I execute?" by pointing at an immutable
    document — so deleting one would make a finished result unexplainable. What a catalogue
    entry owns is the label and the grid; the document belongs to everything that ever ran it.

    A catalogue with no way to remove from it fills with drafts, which is the state this
    project's `strategies` table is already in — and the reason that table is not the shelf.
    """
    entry = session.get(CatalogEntry, entry_id)
    if entry is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="entry not found")
    session.delete(entry)
    session.commit()

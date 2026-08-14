"""`/strategies` — create, read, and version strategies.

The table is append-only (ADR-0010): a strategy is never edited in place, because a backtest
is a claim about an exact definition and editing it would make every past result
unexplainable. So `PUT` does not update — it inserts the next version, linked to its parent.

Validation is the DSL's two layers (`tradeforge_schema`), not restated here: **shape** (the
Pydantic model) then **meaning** (`assert_executable` — a reference to an undeclared indicator,
a target with no stop). The document is otherwise opaque to the API; the database projects
`name`/`schema_version` out of it with generated columns, so the two can never disagree.
"""

import json
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from tradeforge_api.deps import SessionDep
from tradeforge_api.schemas import StrategiesPage, StrategyListItem, StrategyOut
from tradeforge_db.models import Backtest, Strategy
from tradeforge_schema import SemanticValidationError, assert_executable
from tradeforge_schema import Strategy as StrategyDSL

router = APIRouter(tags=["strategies"])

# Declared so the OpenAPI schema is honest about the non-2xx a caller can meet — and so the
# schemathesis contract test holds the code to it.
_Responses = dict[int | str, dict[str, Any]]
_NOT_FOUND: _Responses = {status.HTTP_404_NOT_FOUND: {"description": "strategy not found"}}
_CONFLICT: _Responses = {status.HTTP_409_CONFLICT: {"description": "this name and version exist"}}
# FastAPI answers an unparseable JSON body with 400, before validation ever runs.
_BAD_BODY: _Responses = {status.HTTP_400_BAD_REQUEST: {"description": "malformed request body"}}


def validate_document(document: dict[str, Any]) -> None:
    """Run the DSL's shape and meaning checks, turning either failure into a 422 the client can
    read. `json.loads(exc.json())` is used because a raw `ValidationError.errors()` can carry
    exception objects that will not serialise."""
    try:
        model = StrategyDSL.model_validate(document)
        assert_executable(model)
    except ValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "message": "strategy failed schema validation",
                "errors": json.loads(exc.json()),
            },
        ) from exc
    except SemanticValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"message": "strategy is well-formed but cannot run", "errors": str(exc)},
        ) from exc


def _persist(session: SessionDep, strategy: Strategy) -> Strategy:
    session.add(strategy)
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="a strategy with this name and version already exists",
        ) from exc
    session.refresh(strategy)
    return strategy


@router.post(
    "/strategies",
    response_model=StrategyOut,
    status_code=status.HTTP_201_CREATED,
    responses={**_CONFLICT, **_BAD_BODY},
)
def create_strategy(document: dict[str, Any], session: SessionDep) -> Strategy:
    """The first version of a strategy. Validated, then stored verbatim."""
    validate_document(document)
    return _persist(session, Strategy(definition=document, version=1))


# The largest `offset` the database can be asked for, matching the run log's own bound: past
# `2**63-1` Postgres raises `NumericValueOutOfRange` from inside the driver, which surfaces as a
# 500 on input a client fully controls.
_MAX_OFFSET = 9_223_372_036_854_775_807


@router.get("/strategies", response_model=StrategiesPage)
def list_strategies(
    session: SessionDep,
    q: Annotated[str | None, Query(description="case-insensitive substring of the name")] = None,
    include_generated: Annotated[bool, Query(description="include a grid's own points")] = False,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0, le=_MAX_OFFSET)] = 0,
) -> StrategiesPage:
    """Every strategy lineage, newest first — one row each, not one row per version.

    **The absence of this endpoint is what causes the 409 in the builder.** With no way to ask
    whether a name is taken, the screen decides between `POST` and `PUT` from the only id it
    knows: the one it created in this browser session. A name that exists from any other session
    is therefore invisible to it, and saving under that name is a `POST` that collides. Reading
    is the fix; nothing about writing changes.

    **One row per lineage.** A strategy edited four times is four rows and one strategy, and a
    picker offering all four would make the reader choose a version to answer a question about a
    method. The row carries the latest version, which is the one a new run should use.

    ⚠️ **A grid's own points are left out by default, and without that this list is unusable.**
    A study writes one strategy per combination (`MME9 [period=5, rr=2]`), so a single
    hundred-point search would bury forty-five authored strategies under a hundred generated
    ones. Which those are is **derived, never flagged**: a point is a strategy whose runs belong
    to a study, and the study already records that. A boolean column would be a second place for
    the same truth, and on the day the two disagreed it is the column that would be believed.

    `include_generated` exists because the exclusion is a default, not a judgement — a reader
    who wants to open the exact document a grid ran has to be able to find it.
    """
    generated = (
        select(Backtest.strategy_id)
        .where(Backtest.study_id.is_not(None))
        .distinct()
        .scalar_subquery()
    )

    # The latest version of each lineage. `DISTINCT ON` is Postgres' own way of saying "one row
    # per name", and it is one pass over an index rather than the self-join a portable query
    # would need — the alternative, a subquery of `max(version) GROUP BY name`, reads the table
    # twice to answer a question the first read already had.
    newest = (
        select(Strategy).distinct(Strategy.name).order_by(Strategy.name, Strategy.version.desc())
    )
    if not include_generated:
        newest = newest.where(Strategy.id.not_in(generated))
    if q is not None and q.strip() != "":
        newest = newest.where(Strategy.name.ilike(f"%{q.strip()}%"))

    lineages = newest.subquery()
    rows = session.execute(
        select(
            lineages,
            # One aggregate rather than a query per row: the run count of forty-five strategies
            # in one statement, which is the difference between this list and an N+1.
            select(func.count())
            .select_from(Backtest)
            .where(Backtest.strategy_id == lineages.c.id)
            .scalar_subquery()
            .label("runs"),
        )
        .order_by(lineages.c.created_at.desc())
        .limit(limit)
        .offset(offset)
    ).all()

    total = session.scalar(select(func.count()).select_from(lineages)) or 0

    return StrategiesPage(
        total=total,
        limit=limit,
        offset=offset,
        items=[
            StrategyListItem(
                id=row.id,
                name=row.name,
                version=row.version,
                schema_version=row.schema_version,
                setup=_setup_of(row.definition),
                runs=row.runs,
                created_at=row.created_at,
            )
            for row in rows
        ],
    )


def _setup_of(definition: dict[str, Any]) -> str | None:
    """The named setup a document runs, or `None` for one built from indicators and conditions.

    Read from the document rather than stored beside it: `setup.type` is the DSL's own field,
    and projecting it into a column would be a second copy that could disagree with the
    strategy it describes — the same reason `name` is a generated column here and not a written
    one.
    """
    setup = definition.get("setup")
    if not isinstance(setup, dict):
        return None
    kind = setup.get("type")
    return kind if isinstance(kind, str) else None


@router.get("/strategies/{strategy_id}", response_model=StrategyOut, responses=_NOT_FOUND)
def get_strategy(strategy_id: uuid.UUID, session: SessionDep) -> Strategy:
    strategy = session.get(Strategy, strategy_id)
    if strategy is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="strategy not found")
    return strategy


@router.put(
    "/strategies/{strategy_id}",
    response_model=StrategyOut,
    status_code=status.HTTP_201_CREATED,
    responses={**_NOT_FOUND, **_CONFLICT, **_BAD_BODY},
)
def update_strategy(
    strategy_id: uuid.UUID, document: dict[str, Any], session: SessionDep
) -> Strategy:
    """Editing is a new version, not an update: insert the next version linked to this parent."""
    parent = session.get(Strategy, strategy_id)
    if parent is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="strategy not found")
    validate_document(document)
    successor = Strategy(
        definition=document, version=parent.version + 1, parent_version_id=parent.id
    )
    return _persist(session, successor)

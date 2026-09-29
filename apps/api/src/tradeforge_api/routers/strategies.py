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
from sqlalchemy.dialects.postgresql import aggregate_order_by
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import aliased

from tradeforge_api.deps import SessionDep
from tradeforge_api.runner import timeframe_refusal
from tradeforge_api.schemas import (
    StorableText,
    StrategiesPage,
    StrategyListItem,
    StrategyOut,
)
from tradeforge_db.models import Backtest, CatalogEntry, Strategy, SweepPoint
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


def assert_runnable_at(document: dict[str, Any], timeframe: str) -> None:
    """422 unless this document can run at *this* timeframe — the *deciding* half.

    ⚠️ **A launch settles two timeframes and they are not the same field.** The document carries
    one, the request carries another, and the engine reads each by a different road: the loop
    steps at the request's, while a setup builds its higher-timeframe bars out of the document's.
    They agreed by habit until a screen let somebody pick, and a disagreement under an `htf`
    filter is a backtest whose filter quietly stops filtering — see `runner.timeframe_refusal`
    for the three measured outcomes.

    Refused here rather than in the worker on purpose. A study expands into a hundred runs, and
    a hundred rows reaching `failed` one by one is the same news delivered a hundred times, an
    hour late, with nothing left to fix it on.

    ⚠️ The body is `{message, errors}` with `errors` a **string** — the shape a semantic refusal
    already takes, so the screens that learned to read it in PR-230 need no second format.
    """
    reason = timeframe_refusal(document, timeframe)
    if reason is not None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"message": f"strategy cannot run at {timeframe}", "errors": reason},
        )


def _field_of(loc: tuple[Any, ...]) -> str:
    """The field a Pydantic error is about, without the model names on the way to it.

    Falls back to the whole path when the last segment is an index rather than a name — `1` on
    its own would say nothing, and this is the one case where the path is the information.
    """
    field = loc[-1] if loc else None
    return str(field) if isinstance(field, str) else ".".join(str(part) for part in loc)


def refusal_of(document: dict[str, Any]) -> str | None:
    """Why this document cannot run, or `None` when it can — the *reporting* half of the pair.

    ⚠️ **Deliberately not `validate_document` with the raising taken out.** The two have opposite
    failure policies and folding them into one helper would force one of them to lie. Launching
    *decides*: a point that cannot run is dropped (since 18/09, for the study as for the sweep), and
    a grid where none can is refused with `validate_document`'s structured body, nothing written.
    A preview *reports*: it has to walk every point and come back with all of them, or a person
    fixes one value, asks again, and learns about the next one — which is the round trip the
    preview exists to remove.

    They also want different bodies. `validate_document` carries Pydantic's structured error list,
    which is what a strategy screen renders field by field; a preview needs one sentence it can
    put beside the axis value that caused it.
    """
    try:
        assert_executable(StrategyDSL.model_validate(document))
    except ValidationError as exc:
        # `errors()` first, because a bare `str(exc)` on a Pydantic error is several lines of
        # model paths — unreadable next to a grid value. One clause per failing field, and
        # **every** failing field: a caller fixing them one per round trip is the thing this
        # whole endpoint exists to prevent, and stopping at the first here would put that back
        # one level down.
        #
        # ⚠️ Only the last segment of `loc`. The ones before it are the union branch Pydantic
        # took — `setup.structure_choch.params.stop_buffer` — and `structure_choch` is the name
        # of an internal model, which means nothing to any client. The web already strips it
        # (`settings.ts`, `reasonOf`) from the other place these errors surface, so publishing it
        # here would give one screen two formats for the same failure.
        return "; ".join(f"{_field_of(error['loc'])}: {error['msg']}" for error in exc.errors())
    except SemanticValidationError as exc:
        return str(exc)
    return None


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
def list_strategies(  # noqa: PLR0913 — one query parameter per question a picker asks
    session: SessionDep,
    # Keyword-only from here. FastAPI fills these by name and nothing else calls the
    # endpoint at all, so the `*` costs nothing and removes the question a reader would
    # otherwise have to ask about argument order.
    *,
    q: Annotated[
        StorableText | None, Query(description="case-insensitive substring of the name")
    ] = None,
    name: Annotated[
        StorableText | None, Query(description="exact name, for asking whether one is taken")
    ] = None,
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
    ones. A **sweep** writes one per combination per timeframe, for every entry it was given,
    which is the same failure an order of magnitude larger.

    Which those are is **derived, never flagged**: a point is a strategy whose runs belong
    to a study, and the study already records that. A boolean column would be a second place for
    the same truth, and on the day the two disagreed it is the column that would be believed.

    `include_generated` exists because the exclusion is a default, not a judgement — a reader
    who wants to open the exact document a grid ran has to be able to find it.
    """
    # ⚠️ **A sweep generates points too, and far more of them.** A study writes one strategy per
    # grid combination; a sweep writes one per combination *per timeframe*, for every entry it
    # was given. Left out of this condition, one sweep would bury the authored strategies under
    # its own points — the exact failure `study_id` was added here to prevent, at a larger scale.
    # Derived from the runs, never flagged on the row: a boolean column would be a second place
    # for the same truth, and on the day the two disagreed it is the column that would be
    # believed.
    #
    # ⚠️ **And a sweep's point with no run of its own is still a point** (29/09). A point another
    # point answers (`same_as`) or one refused at its chart is written to `sweep_points` with its
    # strategy and never gets a run — so "has a run in a sweep" let 20 thousand of them through as
    # authored, and his base CHoCH was lost among forty of its own points. `sweep_points` names
    # every point a sweep expanded, run or not. Both read as anti-joins: one pass each over their
    # tables (0.75 s over 7.4 million points, measured), where `NOT IN` a list that size is minutes.
    ran_in_a_grid = (
        select(Backtest.id)
        .where(
            Backtest.strategy_id == Strategy.id,
            Backtest.study_id.is_not(None) | Backtest.sweep_id.is_not(None),
        )
        .exists()
    )
    a_sweep_point = (
        select(SweepPoint.sweep_id).where(SweepPoint.strategy_id == Strategy.id).exists()
    )
    # The shelf's labels for a lineage — any version of it, since an entry pins the version it was
    # saved against and the row here is the latest. The name a person gave it is how he looks for
    # it: the document behind "CHOCH COMPLETO" is called `SCHOCH-20260922-222429`.
    shelved = aliased(Strategy)

    # The latest version of each lineage. `DISTINCT ON` is Postgres' own way of saying "one row
    # per name", and it is one pass over an index rather than the self-join a portable query
    # would need — the alternative, a subquery of `max(version) GROUP BY name`, reads the table
    # twice to answer a question the first read already had.
    newest = (
        select(Strategy).distinct(Strategy.name).order_by(Strategy.name, Strategy.version.desc())
    )
    if not include_generated:
        newest = newest.where(~ran_in_a_grid, ~a_sweep_point)
    # ⚠️ Exact, and separate from `q` rather than a mode of it. The builder's question is "is
    # this name taken?", and answering it with a substring search means filtering in the client
    # and hoping the match landed on the first page — which is the same shape of guess that
    # produces the 409 this endpoint exists to remove.
    if name is not None:
        newest = newest.where(Strategy.name == name)

    # ⚠️ **The authored lineages first, the search over them after** (29/09, measured): left to
    # itself Postgres pushed `q` inside and tested a substring of every strategy's name before the
    # anti-joins cut them down — 2.8 s for "choch" over the real table. A materialised CTE holds
    # the few authored lineages (0.2 s with `sweep_points` indexed by strategy) and the search
    # reads only those: 0.34 s.
    authored = newest.cte("authored").prefix_with("MATERIALIZED")
    found = select(authored)
    if q is not None and q.strip() != "":
        pattern = f"%{q.strip()}%"
        labelled = (
            select(CatalogEntry.id)
            .join(shelved, shelved.id == CatalogEntry.strategy_id)
            .where(shelved.name == authored.c.name, CatalogEntry.name.ilike(pattern))
            .exists()
        )
        found = found.where(authored.c.name.ilike(pattern) | labelled)

    lineages = found.subquery()
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
            select(func.array_agg(aggregate_order_by(CatalogEntry.name, CatalogEntry.name)))
            .join(shelved, shelved.id == CatalogEntry.strategy_id)
            .where(shelved.name == lineages.c.name)
            .scalar_subquery()
            .label("catalog"),
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
                setup=setup_of(row.definition),
                runs=row.runs,
                catalog=list(row.catalog or []),
                created_at=row.created_at,
            )
            for row in rows
        ],
    )


def setup_of(definition: dict[str, Any]) -> str | None:
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

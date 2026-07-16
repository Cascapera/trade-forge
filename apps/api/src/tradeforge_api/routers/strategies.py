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
from typing import Any

from fastapi import APIRouter, HTTPException, status
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError

from tradeforge_api.deps import SessionDep
from tradeforge_api.schemas import StrategyOut
from tradeforge_db.models import Strategy
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


def _validate(document: dict[str, Any]) -> None:
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
    _validate(document)
    return _persist(session, Strategy(definition=document, version=1))


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
    _validate(document)
    successor = Strategy(
        definition=document, version=parent.version + 1, parent_version_id=parent.id
    )
    return _persist(session, successor)

"""Property-based API testing: schemathesis reads the app's own OpenAPI schema and throws
generated requests at every operation, asserting none of them provokes a **server error**.

Against a real Postgres, because the endpoints query it — a random UUID must come back as a
well-formed 404, not a crash. The queue is faked; nothing here enqueues real work.

Only the `not_a_server_error` check runs. The others do not fit this surface: a strategy body
is an *opaque* DSL document (`dict[str, Any]`), so every generated object is "schema-valid" to
OpenAPI yet almost always an invalid strategy — schemathesis would read the honest 422 as a
wrongly-rejected input. The guarantee worth having here is the absolute one: no input, however
malformed, makes a handler 500.

Run locally with:  docker compose up -d  &&  uv run pytest -m integration
"""

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import schemathesis
from hypothesis import settings
from sqlalchemy.orm import Session

from tradeforge_api.config import Settings
from tradeforge_api.main import create_app

pytestmark = pytest.mark.integration


class _FakeQueue:
    async def enqueue_job(self, *args: Any) -> None:
        return None


@pytest.fixture
def api_schema(session_factory: Callable[[], Session], settings: Settings, tmp_path: Path) -> Any:
    app = create_app(
        settings=settings.model_copy(update={"parquet_root": tmp_path}),
        session_factory=session_factory,
        arq_pool=_FakeQueue(),
    )
    return schemathesis.openapi.from_asgi("/openapi.json", app)


schema = schemathesis.pytest.from_fixture("api_schema")


@schema.parametrize()
@settings(max_examples=15, deadline=None)
def test_no_operation_returns_a_server_error(case: Any) -> None:
    case.call_and_validate(checks=(schemathesis.checks.not_a_server_error,))

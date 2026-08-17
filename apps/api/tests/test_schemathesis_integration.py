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
from fastapi.testclient import TestClient
from hypothesis import settings
from sqlalchemy.orm import Session

from tradeforge_api.config import Settings
from tradeforge_api.main import create_app

pytestmark = pytest.mark.integration


class _FakeQueue:
    async def enqueue_job(self, *args: Any, **options: Any) -> None:
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


# --------------------------------------------------------------------------- #
# The same guarantee, pinned — because the fuzzer above is a draw               #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("path", "parameter"),
    [
        ("/strategies", "q"),
        ("/strategies", "name"),
        ("/backtests", "timeframe"),
        ("/backtests", "symbol"),
    ],
)
def test_a_nul_byte_in_a_text_filter_is_refused_and_not_a_crash(
    session_factory: Callable[[], Session],
    settings: Settings,
    tmp_path: Path,
    path: str,
    parameter: str,
) -> None:
    """⚠️ **A deterministic sibling for the draw above, and the reason it exists is history.**

    Postgres text columns cannot hold a NUL byte: the driver raises `DataError`, so a value a
    client fully controls turns a filter into a 500. `_storable` was written for exactly that,
    when schemathesis first drew `?symbol=%00` — and it was attached to `symbol` alone. The
    three siblings kept the plain `str`, and kept the crash, for as long as the fuzzer happened
    not to draw them.

    It drew `?q=%00` today. That is the whole problem with leaving this to the random test: it
    is a *draw*, so a green run is not evidence, and the failure arrives on an unrelated PR
    weeks later. Four parameters, four cases, no chance involved.

    The assertion is 422 rather than "not 500": refusing the value is the contract, and a route
    that started quietly stripping the byte would be searching for something nobody typed.
    """
    app = create_app(
        settings=settings.model_copy(update={"parquet_root": tmp_path}),
        session_factory=session_factory,
        arq_pool=_FakeQueue(),
    )
    with TestClient(app) as client:
        response = client.get(path, params={parameter: "\x00"})

    assert response.status_code == 422, response.text

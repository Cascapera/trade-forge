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
from tradeforge_api.kill_switch import KillSwitch
from tradeforge_api.main import create_app

pytestmark = pytest.mark.integration


class _FakeQueue:
    async def enqueue_job(self, *args: Any, **options: Any) -> None:
        return None


class _FakeSwitchStore:
    """⚠️ **Not an optimisation — the reason this file does not halt live trading.**

    Doubles as the stop store: the fuzzer also presses `POST /live-sessions/{id}/stop`, and
    against the real client that would write a real stop request. Every id it draws is a random
    uuid that 404s today, which is precisely the kind of "safe by accident" this project has
    learned not to rely on — the guarantee should not depend on no session ever matching.

    Postgres is real here on purpose, and Redis is real on this machine too. The fuzzer reads the
    app's own OpenAPI and presses *every* operation, which now includes
    `POST /executor/kill-switch` — against the real client that would be exactly the key the
    executor reads, written to the real Redis, left behind after the run. The next live session
    would then refuse every entry it tried, with nothing raised anywhere and no line in any log
    connecting it to a test.

    That is the shape of the accident that truncated this project's `trades` table on 28/08: a
    test process sharing a write path with production state. Faked here so the fuzzer can press
    the button as hard as it likes.
    """

    def __init__(self) -> None:
        self.keys: dict[str, str] = {}

    def exists(self, *names: Any) -> int:
        return sum(1 for name in names if name in self.keys)

    def get(self, name: Any) -> str | None:
        return self.keys.get(name)

    def mset(self, mapping: Any) -> bool:
        self.keys.update({str(key): str(value) for key, value in mapping.items()})
        return True

    def set(self, name: Any, value: Any) -> bool:
        self.keys[str(name)] = str(value)
        return True


@pytest.fixture
def api_schema(session_factory: Callable[[], Session], settings: Settings, tmp_path: Path) -> Any:
    app = create_app(
        settings=settings.model_copy(update={"parquet_root": tmp_path}),
        session_factory=session_factory,
        arq_pool=_FakeQueue(),
        kill_switch=KillSwitch(_FakeSwitchStore()),
        stop_store=_FakeSwitchStore(),
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
        kill_switch=KillSwitch(_FakeSwitchStore()),
        stop_store=_FakeSwitchStore(),
    )
    with TestClient(app) as client:
        response = client.get(path, params={parameter: "\x00"})

    assert response.status_code == 422, response.text

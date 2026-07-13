"""The wiring, against real containers.

Deselected by default (`-m "not integration"`), so `uv run pytest` stays runnable
without Docker. CI opts in with `-m integration` and real service containers — and
there a refused connection is a red build, never a skip. The moment an unreachable
database is allowed to skip, the suite reports green on a stack that is down.

Run locally with:  docker compose up -d  &&  uv run pytest -m integration
"""

import pytest

from tradeforge_api.config import Settings
from tradeforge_api.health import check_postgres, check_redis

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def settings() -> Settings:
    return Settings()


def test_postgres_answers_a_real_query(settings: Settings) -> None:
    health = check_postgres(settings.postgres_dsn)

    assert health.ok, f"Postgres unreachable: {health.detail}"


def test_redis_answers_a_real_ping(settings: Settings) -> None:
    health = check_redis(settings.redis_url)

    assert health.ok, f"Redis unreachable: {health.detail}"

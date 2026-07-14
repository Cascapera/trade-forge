"""Liveness checks for the services the core depends on.

The connector is injected rather than imported at the call site. That is what
splits the two things worth testing separately: the *logic* (what counts as
healthy, what a failure reports) runs against fakes anywhere, while the *wiring*
(does psycopg actually reach that Postgres) runs against real containers in CI.
A health check that is only ever exercised with mocks is a health check that has
never been checked.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import psycopg
import redis

# The connectors are typed loosely on purpose: the point of the seam is that any
# object honouring the tiny protocol below can stand in during a unit test.
PostgresConnector = Callable[[str], Any]
RedisClientFactory = Callable[[str], Any]


@dataclass(frozen=True, slots=True)
class ServiceHealth:
    """The verdict on one service."""

    name: str
    ok: bool
    detail: str


def _failure(name: str, exc: Exception) -> ServiceHealth:
    # Driver errors arrive as multi-line essays. A health verdict is a single line.
    reason = " ".join(str(exc).split())
    return ServiceHealth(name=name, ok=False, detail=f"{type(exc).__name__}: {reason}")


def check_postgres(dsn: str, *, connect: PostgresConnector = psycopg.connect) -> ServiceHealth:
    """Open a connection and run a trivial query.

    Connecting is not enough: Postgres accepts TCP well before it will answer
    SQL, so a check that stops at the handshake reports healthy during exactly
    the window in which the app would crash.
    """
    try:
        with connect(dsn) as conn:
            conn.execute("SELECT 1")
    except Exception as exc:  # noqa: BLE001 — a health check reports failure, it never raises
        return _failure("postgres", exc)

    return ServiceHealth(name="postgres", ok=True, detail="accepting queries")


def check_redis(
    url: str,
    *,
    client_factory: RedisClientFactory = redis.Redis.from_url,
) -> ServiceHealth:
    """Ping Redis and hang up."""
    try:
        client = client_factory(url)
        try:
            client.ping()
        finally:
            client.close()
    except Exception as exc:  # noqa: BLE001 — see check_postgres
        return _failure("redis", exc)

    return ServiceHealth(name="redis", ok=True, detail="responding to PING")

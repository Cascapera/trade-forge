"""Health-check *logic*, against fakes. The wiring is proved in the integration run."""

from types import TracebackType
from typing import Self

from tradeforge_api.health import check_postgres, check_redis


class FakeConnection:
    """Stands in for a psycopg connection: a context manager that can execute."""

    def __init__(self) -> None:
        self.queries: list[str] = []

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Returning None (not False) tells mypy this never swallows an exception."""

    def execute(self, query: str) -> None:
        self.queries.append(query)


class FakeRedis:
    def __init__(self) -> None:
        self.pinged = False
        self.closed = False

    def ping(self) -> bool:
        self.pinged = True
        return True

    def close(self) -> None:
        self.closed = True


def test_postgres_is_healthy_only_after_a_query_succeeds() -> None:
    connection = FakeConnection()

    health = check_postgres("postgresql://x", connect=lambda _: connection)

    assert health.ok
    assert health.name == "postgres"
    # The check must not stop at the handshake: TCP is accepted before SQL is.
    assert connection.queries == ["SELECT 1"]


def test_postgres_reports_failure_instead_of_raising() -> None:
    def refuse(_: str) -> FakeConnection:
        raise ConnectionRefusedError("no route to host")

    health = check_postgres("postgresql://x", connect=refuse)

    assert not health.ok
    assert health.detail == "ConnectionRefusedError: no route to host"


def test_failure_detail_collapses_to_a_single_line() -> None:
    """psycopg reports a multi-line essay; a health verdict is one line."""

    def refuse(_: str) -> FakeConnection:
        raise ConnectionRefusedError("connection failed:\n  - host: 'localhost'\n  - retrying")

    health = check_postgres("postgresql://x", connect=refuse)

    assert "\n" not in health.detail
    assert health.detail == (
        "ConnectionRefusedError: connection failed: - host: 'localhost' - retrying"
    )


def test_redis_is_healthy_when_it_answers_ping() -> None:
    client = FakeRedis()

    health = check_redis("redis://x", client_factory=lambda _: client)

    assert health.ok
    assert client.pinged
    assert client.closed


def test_redis_hangs_up_even_when_the_ping_fails() -> None:
    """A leaked connection per failed health check is a slow-motion outage."""
    client = FakeRedis()

    def explode() -> bool:
        raise TimeoutError("timed out")

    client.ping = explode  # type: ignore[method-assign]

    health = check_redis("redis://x", client_factory=lambda _: client)

    assert not health.ok
    assert health.detail == "TimeoutError: timed out"
    assert client.closed

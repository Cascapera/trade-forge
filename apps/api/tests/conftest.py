"""Fixtures for the API tests that need a real Postgres.

Only the integration tests ask for these; the unit tests never do, so `uv run pytest` still
runs with no Docker anywhere. Mirrors the `packages/db` conftest — one migrated database per
session, truncated to a known state before each test.
"""

from collections.abc import Callable, Iterator

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from tradeforge_api.config import Settings
from tradeforge_db.migrate import upgrade
from tradeforge_db.session import create_db_engine, create_session_factory

TABLES_CHILD_FIRST = (
    "trades",
    "backtest_metrics",
    "backtests",
    "strategies",
    "datasets",
    "instruments",
)


@pytest.fixture(scope="session")
def settings() -> Settings:
    return Settings()


@pytest.fixture(scope="session")
def migrated_engine(settings: Settings) -> Iterator[Engine]:
    """A database at head, migrated once for the whole session."""
    upgrade("head", dsn=settings.sqlalchemy_dsn)
    engine = create_db_engine(settings.sqlalchemy_dsn)
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def session_factory(migrated_engine: Engine) -> Callable[[], Session]:
    """A session factory over an emptied database. Truncating before the test means a failure
    leaves its rows behind to inspect, and the next test still starts from nothing."""
    with migrated_engine.begin() as connection:
        connection.execute(
            text(f"TRUNCATE {', '.join(TABLES_CHILD_FIRST)} RESTART IDENTITY CASCADE")
        )
    return create_session_factory(migrated_engine)


@pytest.fixture
def session(session_factory: Callable[[], Session]) -> Iterator[Session]:
    db = session_factory()
    try:
        yield db
    finally:
        db.rollback()
        db.close()

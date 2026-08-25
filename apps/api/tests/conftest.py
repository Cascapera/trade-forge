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
    # ⚠️ Before `strategies` and `instruments`, which it points at, and after `trades`, which
    # points at it. Missing here until the first API test wrote one — `packages/db`'s copy has
    # had it since the table existed, and the two lists drifted. A session row left behind is
    # not inert: it is a parent the next test's trades could attach to.
    "live_sessions",
    "backtest_metrics",
    "backtests",
    "baskets",
    # Named rather than left to the CASCADE that would reach it from `strategies` anyway. A
    # tuple whose name says "child first" and which in practice relies on a cascade is one that
    # breaks the day the cascade changes — and it breaks as a test leaving rows behind for the
    # next one, which is the hardest kind of failure to attribute.
    "studies",
    "strategies",
    "datasets",
    "instruments",
    # ⚠️ Not a child of anything, and listed anyway. `broker_symbols` deliberately has no
    # foreign key — that is what lets a sync replace it wholesale — so no CASCADE reaches it
    # and a test that syncs would leak its rows into the next one.
    "broker_symbols",
    # The same argument, and the same absence of a foreign key. `symbol_history` is keyed on
    # (symbol, timeframe), so a row left behind is not inert — it is the answer the next test
    # reads. It was missed when the table was added, which is what this comment is for.
    "symbol_history",
    "collections",
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

"""Fixtures for the tests that need a real Postgres.

Only the integration tests touch these. The unit tests in this directory never ask
for them, so `uv run pytest` still runs with no Docker anywhere.
"""

from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from tradeforge_db.config import PostgresSettings
from tradeforge_db.migrate import upgrade
from tradeforge_db.session import create_db_engine, create_session_factory

# Children first: TRUNCATE ... CASCADE would do this for us, but naming the order
# makes the dependency between the tables visible where someone will read it.
TABLES_CHILD_FIRST = (
    "trades",
    # Before `strategies` and `instruments`, which it points at, and after `trades`, which
    # points at it. Listed for the same reason `symbol_history` is: a session row left
    # behind is not inert, it is a parent the next test's trades could attach to.
    "live_sessions",
    "backtest_metrics",
    "backtests",
    "baskets",
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
def dsn() -> str:
    return PostgresSettings().sqlalchemy_dsn


@pytest.fixture(scope="session")
def migrated_engine(dsn: str) -> Iterator[Engine]:
    """A database at head. Migrated once for the whole session, not once per test."""
    upgrade("head", dsn=dsn)

    engine = create_db_engine(dsn)
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def session(migrated_engine: Engine) -> Iterator[Session]:
    """An empty database and an open session.

    Truncating before rather than after means a failed test leaves its rows behind to
    be inspected — and the next test still starts from a known state.
    """
    with migrated_engine.begin() as connection:
        connection.execute(
            text(f"TRUNCATE {', '.join(TABLES_CHILD_FIRST)} RESTART IDENTITY CASCADE")
        )

    db = create_session_factory(migrated_engine)()
    try:
        yield db
    finally:
        db.rollback()
        db.close()

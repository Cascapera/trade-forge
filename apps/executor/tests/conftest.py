"""Fixtures for the executor tests that need a real Postgres.

Only `test_ledger_integration.py` asks for these; the safeguards, the router and the wire format
run with no database anywhere, which is the whole point of how this service is split.

Mirrors `packages/db` and `apps/api` — one migrated database per session, emptied before each
test through the shared helper. The list is short on purpose: this service writes to exactly one
table, and the others are here because `CASCADE` reaches them from `instruments`.
"""

from collections.abc import Iterator

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from tradeforge_db.config import PostgresSettings
from tradeforge_db.migrate import upgrade
from tradeforge_db.session import create_db_engine, create_session_factory
from tradeforge_db.testing import truncate

# ⚠️ `order_audit` first, and it is the reason `truncate` exists rather than a bare statement:
# its append-only trigger refuses TRUNCATE, and `CASCADE` from `instruments` reaches it whether
# or not anybody names it. See `tradeforge_db.testing`.
TABLES_CHILD_FIRST = (
    "order_audit",
    "trades",
    "live_sessions",
    "strategies",
    "instruments",
)


@pytest.fixture(scope="session")
def dsn() -> str:
    return PostgresSettings().sqlalchemy_dsn


@pytest.fixture(scope="session")
def migrated_engine(dsn: str) -> Iterator[Engine]:
    """A database at head, migrated once for the whole session."""
    upgrade("head", dsn=dsn)
    engine = create_db_engine(dsn)
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def session(migrated_engine: Engine) -> Iterator[Session]:
    """A session over an emptied database.

    Truncating *before* the test means a failure leaves its rows behind to inspect — including
    the audit rows, which are the ones worth looking at when something went wrong.
    """
    with migrated_engine.begin() as connection:
        truncate(connection, TABLES_CHILD_FIRST)

    db = create_session_factory(migrated_engine)()
    try:
        yield db
    finally:
        db.rollback()
        db.close()

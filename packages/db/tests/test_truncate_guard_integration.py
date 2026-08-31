"""The refusal to empty a database that is not named as disposable.

⚠️ **Written because prose had already been tried and had already failed.** Three suites carried
the warning — *"the variable is not optional; without it the integration suite truncates whatever
database the environment points at"* — and on 2026-08-31 the developer's `tradeforge` database was
found holding **0 trades and 0 backtests** against 24 001 trades three days earlier, with an
integration run's own fixtures left behind in it. The second occurrence; the first took his
backtests on 2026-08-04.

**The target here is a real database, not a fake connection**, and that is the point of the file.
The rule is about what `current_database()` answers on the server that is about to execute the
statement — a fake would be a test of a `str.endswith` I could read in the source.
"""

from collections.abc import Iterator

import pytest
from sqlalchemy import URL, Engine, create_engine, text

from tradeforge_db.config import PostgresSettings
from tradeforge_db.session import create_db_engine
from tradeforge_db.testing import TEST_DATABASE_SUFFIX, NotDisposableError, truncate

from .conftest import TABLES_CHILD_FIRST

pytestmark = pytest.mark.integration

# The maintenance database. Always present on any Postgres, owned by nobody's data, and — the
# reason it is the right target — **its name does not end in `_test`**, so it is a database this
# suite genuinely must not empty. Nothing here writes to it.
MAINTENANCE = "postgres"


@pytest.fixture
def maintenance_engine() -> Iterator[Engine]:
    engine = create_db_engine(PostgresSettings(postgres_db=MAINTENANCE).sqlalchemy_dsn)
    try:
        yield engine
    finally:
        engine.dispose()


def test_truncate_refuses_a_database_not_named_as_disposable(maintenance_engine: Engine) -> None:
    """⚠️ **`NotDisposableError` rather than any error at all is the assertion**, and it
    is what proves the guard runs *before* the trigger is disabled.

    `postgres` has no `order_audit` table. So a guard placed after `_UNGUARD` — the ordinary way
    to write this, and the wrong one — would raise `ProgrammingError: relation "order_audit" does
    not exist` here, and a test that only asked "does it raise" would go green against it. The
    ordering matters for real: the unguard switches off the trigger that makes the audit trail
    append-only, and a refusal that fires afterwards leaves a **live** database with its audit
    guard down, which is worse than the state it was refusing to create.
    """
    with maintenance_engine.begin() as connection:
        with pytest.raises(NotDisposableError) as caught:
            truncate(connection, TABLES_CHILD_FIRST)

    message = str(caught.value)
    assert MAINTENANCE in message, "the refusal does not say which database it refused"
    assert f"POSTGRES_DB={MAINTENANCE}{TEST_DATABASE_SUFFIX}" in message, (
        "the refusal does not say how to fix it; a guard that only says no teaches the reader "
        "to reach for the flag that turns it off"
    )


def test_nothing_is_executed_before_the_refusal(maintenance_engine: Engine) -> None:
    """The other half of the ordering, asserted where it can be seen rather than inferred.

    ⚠️ This is not a restatement of the test above. That one shows *which* exception comes out;
    this one shows the database was not touched on the way to it. They come apart in exactly the
    implementation worth worrying about: an unguard, then a refusal, then a `finally` that
    re-guards would satisfy the first test and still leave a real window — narrow, and open — in
    which `order_audit` is not append-only on a database nobody meant to touch.

    ⚠️ **The read after the refusal is the assertion, and it works by Postgres' own rule**: a
    statement that errors poisons the whole transaction, so every later one on that connection is
    refused until rollback. Against an implementation that reached `_UNGUARD` — which fails here,
    because `postgres` has no `order_audit` — this line raises instead of answering. Named as
    "nothing is executed" rather than "the trigger is left alone" because the maintenance database
    has no such trigger to leave alone; what is proven is the step before it.
    """
    with maintenance_engine.connect() as connection:
        before = connection.execute(text("SELECT current_database()")).scalar_one()

        with pytest.raises(NotDisposableError):
            truncate(connection, TABLES_CHILD_FIRST)

        after = connection.execute(text("SELECT current_database()")).scalar_one()

    assert after == before == MAINTENANCE


def test_the_name_is_asked_of_the_server_not_of_the_connection_url() -> None:
    """⚠️ **The one fixture where the URL and the database disagree**, which is the only place
    the two readings can be told apart.

    `connection.engine.url.database` is what somebody *believes* the connection points at;
    `current_database()` is what the `TRUNCATE` will actually empty. Everywhere else in this repo
    they agree — every engine is built from one DSN — so a guard reading the URL passes the whole
    suite, and it was measured surviving every other test in this file.

    They come apart wherever something between the client and the server rewrites the target: a
    pooler with a database alias is the ordinary case. Here it is arranged with `connect_args`,
    which is the smallest honest way to build the disagreement, and both halves are asserted
    before the refusal so the scenario cannot quietly stop being one.
    """
    settings = PostgresSettings()
    lying = create_engine(
        URL.create(
            "postgresql+psycopg",
            username=settings.postgres_user,
            password=settings.postgres_password,
            host=settings.postgres_host,
            port=settings.postgres_port,
            database=f"fake{TEST_DATABASE_SUFFIX}",
        ),
        connect_args={"dbname": MAINTENANCE},
    )
    try:
        with lying.connect() as connection:
            assert lying.url.database == f"fake{TEST_DATABASE_SUFFIX}", "the URL stopped lying"
            landed = connection.execute(text("SELECT current_database()")).scalar_one()
            assert landed == MAINTENANCE, (
                "the connection landed where its URL said, so this scenario no longer separates "
                "reading the URL from asking the server"
            )

            with pytest.raises(NotDisposableError):
                truncate(connection, TABLES_CHILD_FIRST)
    finally:
        lying.dispose()


def test_the_suite_s_own_database_is_named_as_disposable(dsn: str) -> None:
    """⚠️ **The vacuity check, and it belongs here.** Every other integration test in this repo
    truncates in its fixture, so they collectively prove the accepting branch — but only while
    the suite is actually pointed at a `*_test` database. If this stops holding, the whole
    integration suite fails in its fixtures with a message about disposability, and this test is
    the one that says why in a sentence instead of two hundred times in a stack trace.
    """
    assert PostgresSettings().postgres_db.endswith(TEST_DATABASE_SUFFIX), (
        f"the integration suite is pointed at {PostgresSettings().postgres_db!r}, which "
        f"`truncate` refuses to empty. Run it with POSTGRES_DB=<name>{TEST_DATABASE_SUFFIX}."
    )
    assert dsn.endswith(PostgresSettings().postgres_db), "the DSN and the settings disagree"

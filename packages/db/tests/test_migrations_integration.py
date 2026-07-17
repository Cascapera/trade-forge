"""Migrations, against a real Postgres.

Run locally with:  docker compose up -d  &&  uv run pytest -m integration

A migration that has only ever been reasoned about is a migration that has never
been run. In particular, `downgrade` is the half nobody exercises until the night
they need it — so it is exercised here, on every push.
"""

from collections.abc import Iterator

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import Engine, create_engine, inspect

from tradeforge_db.base import Base
from tradeforge_db.migrate import downgrade, upgrade

pytestmark = pytest.mark.integration

EXPECTED_TABLES = {
    "instruments",
    "datasets",
    "strategies",
    "backtests",
    "backtest_metrics",
    "trades",
}


def test_upgrade_creates_every_table(migrated_engine: Engine) -> None:
    tables = set(inspect(migrated_engine).get_table_names())

    assert tables >= EXPECTED_TABLES
    # Alembic's bookkeeping table. Without it the database has no idea what it has run.
    assert "alembic_version" in tables


def test_the_models_are_exactly_what_the_migration_built(migrated_engine: Engine) -> None:
    """The drift test — the strongest assertion in this package.

    A hand-written migration and a set of models are two descriptions of one schema, and
    nothing forces them to agree. Forget a CHECK in the migration and every unit test
    still passes, because the unit tests read the models; the constraint simply is not
    there in production. Here Alembic diffs the live database against the metadata, and
    an empty diff is the only acceptable answer.
    """
    with migrated_engine.connect() as connection:
        context = MigrationContext.configure(connection, opts={"compare_type": True})
        differences = compare_metadata(context, Base.metadata)

    assert differences == []


def test_the_append_only_trigger_is_installed(migrated_engine: Engine) -> None:
    """The rule that makes `strategies` immutable lives in the database, so look there."""
    with migrated_engine.connect() as connection:
        triggers = connection.exec_driver_sql(
            "SELECT tgname FROM pg_trigger WHERE NOT tgisinternal"
        ).scalars()

    assert "strategies_no_update" in set(triggers)


@pytest.fixture
def _restore_head(dsn: str) -> Iterator[None]:
    """Put the database back at head no matter how the round-trip test ends."""
    yield
    upgrade("head", dsn=dsn)


@pytest.mark.usefixtures("_restore_head")
def test_downgrade_unwinds_to_nothing_and_upgrade_rebuilds_it(dsn: str) -> None:
    """upgrade → downgrade → upgrade, for real.

    This is the test that keeps a rollback plan honest. It catches the two failures that
    only ever show up under pressure: a table dropped before the table that references
    it, and a trigger or function left behind by `downgrade` that makes the *next*
    `upgrade` die with "already exists".
    """
    downgrade("base", dsn=dsn)

    engine = create_engine(dsn)
    try:
        remaining = set(inspect(engine).get_table_names())
        # `alembic_version` survives on purpose: it is Alembic's own bookkeeping, not
        # part of our schema. Everything of ours must be gone.
        assert remaining - {"alembic_version"} == set()

        leftovers = (
            engine.connect()
            .exec_driver_sql(
                "SELECT proname FROM pg_proc WHERE proname = 'strategies_reject_update'"
            )
            .scalars()
        )
        assert list(leftovers) == [], "downgrade left the trigger function behind"

        upgrade("head", dsn=dsn)

        assert set(inspect(engine).get_table_names()) >= EXPECTED_TABLES
    finally:
        engine.dispose()

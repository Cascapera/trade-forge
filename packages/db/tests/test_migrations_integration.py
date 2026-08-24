"""Migrations, against a real Postgres.

Run locally with:  docker compose up -d  &&  uv run pytest -m integration

A migration that has only ever been reasoned about is a migration that has never
been run. In particular, `downgrade` is the half nobody exercises until the night
they need it — so it is exercised here, on every push.
"""

import re
from collections.abc import Iterator

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import CheckConstraint, Engine, Enum, create_engine, inspect, text

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
    "live_sessions",
}


def test_upgrade_creates_every_table(migrated_engine: Engine) -> None:
    tables = set(inspect(migrated_engine).get_table_names())

    assert tables >= EXPECTED_TABLES
    # Alembic's bookkeeping table. Without it the database has no idea what it has run.
    assert "alembic_version" in tables


def enum_check_names() -> set[str]:
    """The CHECK constraints Postgres holds only because a non-native `Enum` asked for it.

    `models._enum` builds every enum column with `native_enum=False, create_constraint=True`,
    so each one becomes an ordinary `CHECK (col IN (...))` named `ck_<table>_<enum name>`.
    Derived from the metadata rather than listed, so an enum added tomorrow is covered and a
    hand-written rule never is.
    """
    return {
        f"ck_{table.name}_{column.type.name}"
        for table in Base.metadata.tables.values()
        for column in table.columns
        if isinstance(column.type, Enum) and not column.type.native_enum and column.type.name
    }


def test_the_models_are_exactly_what_the_migration_built(migrated_engine: Engine) -> None:
    """The drift test — the strongest assertion in this package.

    A hand-written migration and a set of models are two descriptions of one schema, and
    nothing forces them to agree. Forget a CHECK in the migration and every unit test
    still passes, because the unit tests read the models; the constraint simply is not
    there in production. Here Alembic diffs the live database against the metadata, and
    an empty diff is the only acceptable answer.

    ⚠️ **Enum-generated CHECKs are excluded, and only those.** Alembic 1.19 began comparing
    CHECK constraints — which is how it found the thirty-one doubled names `rev_0013` fixed —
    but it cannot match the constraint a non-native `Enum` produces back to the `Enum` in the
    metadata, so it reports all ten as constraints to remove. The schema is right and the tool
    cannot see it. The exclusion is computed from the metadata, not spelled out, so it covers
    exactly the constraints nobody wrote by hand and stays sharp on every rule somebody did.
    """
    ignored = enum_check_names()

    def include_object(
        obj: object, name: str | None, type_: str, reflected: bool, compare_to: object
    ) -> bool:
        return not (type_ == "check_constraint" and name in ignored)

    with migrated_engine.connect() as connection:
        context = MigrationContext.configure(
            connection, opts={"compare_type": True, "include_object": include_object}
        )
        differences = compare_metadata(context, Base.metadata)

    assert differences == []


def test_the_enum_exclusion_covers_exactly_the_generated_checks(migrated_engine: Engine) -> None:
    """A filter that silenced more than it claimed would make the drift test decorative.

    So the excluded names are checked against the database twice over: every one of them is
    really there, and none of them is a rule anybody wrote — a hand-written check would have a
    name the metadata does not derive from an enum type.
    """
    ignored = enum_check_names()
    assert ignored, "no enum checks derived at all: the derivation is wrong, not the schema"

    with migrated_engine.connect() as connection:
        present = {
            row[0]
            for row in connection.execute(
                text(
                    "SELECT c.conname FROM pg_constraint c "
                    "JOIN pg_class t ON t.oid = c.conrelid "
                    "JOIN pg_namespace n ON n.oid = t.relnamespace "
                    "WHERE n.nspname = current_schema() AND c.contype = 'c'"
                )
            )
        }

    assert ignored <= present, f"excluded a check the database does not have: {ignored - present}"
    hand_written = {name for name in present if name.startswith("ck_")} - ignored
    assert len(hand_written) > 40, (
        f"only {len(hand_written)} hand-written checks left under scrutiny; the filter is "
        "swallowing rules it was not meant to"
    )


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


# --------------------------------------------------------------------------- #
# Constraint names, and the prefix that was written twice (rev_0013)            #
# --------------------------------------------------------------------------- #

# `ck_%(table_name)s_%(constraint_name)s` interpolates the name it is handed, so a migration
# that writes the prefix itself gets it twice. This is the pattern that catches it:
# `ck_backtests_ck_backtests_...`.
_DOUBLED_PREFIX = re.compile(r"^(ck|uq|ix|fk)_(?P<table>\w+?)_(ck|uq|ix|fk)_(?P=table)_")


def test_no_constraint_in_the_database_carries_its_prefix_twice(migrated_engine: Engine) -> None:
    """The regression guard for rev_0013, stated against the database rather than the models.

    ⚠️ The models could never have caught this. They declare `ck_backtests_failed_needs_error`
    and always did; it was `rev_0001` that handed the convention a name with the prefix already
    in it, and the database that ended up with `ck_backtests_ck_backtests_failed_needs_error`.
    Thirty-one of them, in every database this project has ever built, invisible for a year
    because Alembic 1.18 does not compare CHECK constraints and 1.19 does.
    """
    with migrated_engine.connect() as connection:
        names = [
            row[0]
            for row in connection.execute(
                text(
                    "SELECT c.conname FROM pg_constraint c "
                    "JOIN pg_class t ON t.oid = c.conrelid "
                    "JOIN pg_namespace n ON n.oid = t.relnamespace "
                    "WHERE n.nspname = current_schema() ORDER BY 1"
                )
            )
        ]

    assert names, "no constraints at all: the query is wrong, not the schema"
    doubled = [name for name in names if _DOUBLED_PREFIX.match(name)]
    assert doubled == [], f"{len(doubled)} constraint(s) carry their prefix twice: {doubled[:5]}"


def test_the_database_and_the_models_agree_on_every_check_name(migrated_engine: Engine) -> None:
    """The sharper half: absence of doubling is not the same as agreement.

    A name could be free of the doubled pattern and still not be the one the models declare —
    a migration that invented its own spelling, say. This compares the two sets outright, which
    is the property `drop_constraint` depends on: the ORM's name has to be the name that is
    actually there.
    """
    with migrated_engine.connect() as connection:
        in_database = {
            row[0]
            for row in connection.execute(
                text(
                    "SELECT c.conname FROM pg_constraint c "
                    "JOIN pg_class t ON t.oid = c.conrelid "
                    "JOIN pg_namespace n ON n.oid = t.relnamespace "
                    "WHERE n.nspname = current_schema() AND c.contype = 'c'"
                )
            )
        }
    # `str(...)` because SQLAlchemy types an unnamed constraint's `.name` as a sentinel, not
    # as `str` — the `if` already excluded those, and this tells mypy so.
    in_models = {
        str(constraint.name)
        for table in Base.metadata.tables.values()
        for constraint in table.constraints
        if isinstance(constraint, CheckConstraint) and constraint.name
    }

    # ⚠️ Only one direction is asserted. Postgres materialises a CHECK for every NOT NULL and
    # for every non-native enum, so the database legitimately holds names the models never
    # declared; the models holding a name the database does not is the failure.
    missing = in_models - in_database
    assert missing == set(), f"the models name checks the database does not have: {sorted(missing)}"

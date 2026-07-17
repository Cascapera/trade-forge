"""The migration graph, inspected without a database."""

import pytest

from tradeforge_db.migrate import MIGRATIONS_DIR, alembic_config, heads


def test_there_is_exactly_one_head() -> None:
    """Two heads mean two branches of history, and Alembic refuses to upgrade into that.

    It happens when two feature branches each add a migration and both get merged. The
    fix is a merge revision — but you want to find out here, in a test that runs on every
    push, not in the deploy that was supposed to ship on Friday.
    """
    assert len(heads()) == 1


def test_the_config_needs_no_ini_file_and_no_working_directory() -> None:
    """The deployed container has neither."""
    config = alembic_config()

    assert config.get_main_option("script_location") == str(MIGRATIONS_DIR)
    assert config.config_file_name is None


def test_a_dsn_with_a_percent_sign_is_not_interpolated() -> None:
    """Alembic runs `%`-interpolation over ini options — so the DSN travels beside them.

    A password containing `%` is legal, and passing it through `sqlalchemy.url` would
    either mangle it or crash ConfigParser. `attributes` is a plain dict.
    """
    dsn = "postgresql+psycopg://trader:100%25sure@localhost:5432/forge"

    config = alembic_config(dsn)

    assert config.attributes["dsn"] == dsn


@pytest.mark.parametrize("required", ["upgrade", "downgrade"])
def test_every_migration_can_be_undone(required: str) -> None:
    """A migration with no `downgrade` is a deploy with no rollback plan.

    Read as source rather than executed, so the check is cheap and runs everywhere. The
    integration suite proves the rollback actually *works*; this proves nobody forgot to
    write one.
    """
    revisions = list(MIGRATIONS_DIR.glob("versions/*.py"))

    assert revisions, "no migrations found — the glob is wrong"
    for revision in revisions:
        source = revision.read_text(encoding="utf-8")
        assert f"def {required}() -> None:" in source, f"{revision.name} has no {required}()"
        # `pass` in a downgrade is the same thing as no downgrade, wearing a hat.
        assert "\n    pass\n" not in source, f"{revision.name} has an empty body"

"""Running migrations from Python, without shelling out to the `alembic` CLI.

The API container, the test suite and the developer's terminal all need to bring a
database to head. Wrapping Alembic here means they do it the same way, and that the
test suite can assert on the thing that actually ships.
"""

from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def alembic_config(dsn: str | None = None) -> Config:
    """Build Alembic's config in code. No `alembic.ini` lookup, no working directory.

    The DSN travels in `attributes`, not in the `sqlalchemy.url` option, because
    Alembic's option values go through ConfigParser's `%`-interpolation: a password
    containing a percent sign would be mangled, or would crash the parser outright.
    `attributes` is a plain dict — nothing interprets what we put in it.

    With no DSN, `env.py` falls back to the environment. That is what lets this
    function be called with no configuration at all when the caller only wants to
    read the revision graph.
    """
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS_DIR))
    if dsn is not None:
        config.attributes["dsn"] = dsn
    return config


def upgrade(revision: str = "head", *, dsn: str | None = None) -> None:
    """Apply migrations forward."""
    command.upgrade(alembic_config(dsn), revision)


def downgrade(revision: str, *, dsn: str | None = None) -> None:
    """Roll migrations back. `base` unwinds the database to empty."""
    command.downgrade(alembic_config(dsn), revision)


def heads() -> tuple[str, ...]:
    """The head revisions of the migration graph. Touches no database.

    There must be exactly one. Two heads mean two branches of history — merge two
    feature branches that each added a migration and Alembic will refuse to upgrade,
    usually in CI, usually on a Friday. A unit test asserts this stays at one.
    """
    return tuple(ScriptDirectory.from_config(alembic_config()).get_heads())

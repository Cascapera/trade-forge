"""Alembic's entry point.

Loaded by Alembic itself, by path — never imported as a module. The DSN arrives via
`config.attributes` (see `migrate.alembic_config`) and falls back to the environment,
so the same file serves the test suite, the deployed container and the developer
running `alembic revision --autogenerate` by hand.
"""

from alembic import context
from sqlalchemy import engine_from_config, pool

import tradeforge_db.models  # noqa: F401 — side effect: registers the tables on Base.metadata
from tradeforge_db.base import Base
from tradeforge_db.config import PostgresSettings

config = context.config

# What `--autogenerate` compares the live database against.
target_metadata = Base.metadata


def _dsn() -> str:
    configured = config.attributes.get("dsn")
    if isinstance(configured, str):
        return configured
    return PostgresSettings().sqlalchemy_dsn


def run_migrations_offline() -> None:
    """Emit SQL to stdout instead of running it (`alembic upgrade head --sql`).

    This is how a DBA reviews a migration before it touches production.
    """
    context.configure(
        url=_dsn(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run the migrations against a live database."""
    engine = engine_from_config(
        {"sqlalchemy.url": _dsn()},
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            # Without this, changing a column from NUMERIC(20,8) to NUMERIC(20,10)
            # autogenerates as *nothing at all* — Alembic ignores type changes by
            # default, and the silent no-op is worse than a noisy false positive.
            compare_type=True,
        )

        # DDL in Postgres is transactional: if the third statement of a migration
        # fails, the first two roll back with it. There is no half-applied schema —
        # which is the property that makes `downgrade` trustworthy in the first place.
        with context.begin_transaction():
            context.run_migrations()

    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()

"""Where the database lives, read from the environment (12-factor).

This is the *only* place in the system that assembles a Postgres connection
string. The API, the collector, the executor and Alembic all read it from here —
a second hand-built DSN somewhere else is how a service ends up talking to the
wrong database while every log line insists it is fine.
"""

from urllib.parse import quote_plus

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class PostgresSettings(BaseSettings):
    """Connection settings for Postgres."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    postgres_host: str = "localhost"
    postgres_port: int = 5432
    postgres_db: str = "tradeforge"
    postgres_user: str = "tradeforge"

    # Deliberately no default. A default password is how a database ends up
    # reachable with credentials that are published in the repository — the app
    # must refuse to start rather than silently fall back to a known secret.
    postgres_password: str = Field(min_length=1)

    @property
    def postgres_dsn(self) -> str:
        """libpq connection string, for psycopg used directly. Never logged."""
        return f"postgresql://{self._credentials}@{self._authority}/{self.postgres_db}"

    @property
    def sqlalchemy_dsn(self) -> str:
        """SQLAlchemy URL. The `+psycopg` suffix pins the driver to psycopg 3.

        Without it SQLAlchemy defaults to psycopg2, which is not installed — and
        the failure would surface as an import error at the first query rather
        than as a configuration mistake at startup.
        """
        return f"postgresql+psycopg://{self._credentials}@{self._authority}/{self.postgres_db}"

    @property
    def _credentials(self) -> str:
        # A password is free-form text; `@`, `/` and `:` are structural characters
        # in a URL. Unescaped, a perfectly legal password silently truncates the
        # host — percent-encoding is what keeps the two layers from colliding.
        return f"{quote_plus(self.postgres_user)}:{quote_plus(self.postgres_password)}"

    @property
    def _authority(self) -> str:
        return f"{self.postgres_host}:{self.postgres_port}"

"""Configuration, read from the environment (12-factor).

Every value comes from an environment variable, and `.env` exists only to make a
developer's shell convenient. Nothing here reaches for a config file at runtime,
which is what lets the same image run in dev, staging and production unchanged.
"""

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Connection settings for the services the core depends on."""

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

    redis_host: str = "localhost"
    redis_port: int = 6379

    @property
    def postgres_dsn(self) -> str:
        """libpq connection string. Never logged: it carries the password."""
        return (
            f"postgresql://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @property
    def redis_url(self) -> str:
        return f"redis://{self.redis_host}:{self.redis_port}/0"

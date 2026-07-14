"""Configuration, read from the environment (12-factor).

Every value comes from an environment variable, and `.env` exists only to make a
developer's shell convenient. Nothing here reaches for a config file at runtime,
which is what lets the same image run in dev, staging and production unchanged.

The Postgres half is inherited from `tradeforge_db`, not restated. Two services
assembling their own connection strings is two chances to drift — and the failure
mode of a drifted DSN is not a crash, it is an application quietly reading the
wrong database.
"""

from tradeforge_db.config import PostgresSettings


class Settings(PostgresSettings):
    """Connection settings for the services the API depends on."""

    redis_host: str = "localhost"
    redis_port: int = 6379

    @property
    def redis_url(self) -> str:
        return f"redis://{self.redis_host}:{self.redis_port}/0"

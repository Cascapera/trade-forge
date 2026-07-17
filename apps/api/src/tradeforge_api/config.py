"""Configuration, read from the environment (12-factor).

Every value comes from an environment variable, and `.env` exists only to make a
developer's shell convenient. Nothing here reaches for a config file at runtime,
which is what lets the same image run in dev, staging and production unchanged.

The Postgres half is inherited from `tradeforge_db`, not restated. Two services
assembling their own connection strings is two chances to drift — and the failure
mode of a drifted DSN is not a crash, it is an application quietly reading the
wrong database.
"""

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

from tradeforge_db.config import PostgresSettings


class RedisConfig(BaseSettings):
    """Where Redis lives — host and port, nothing secret.

    Deliberately free-standing rather than folded into `Settings`. The worker builds arq's
    connection settings in its class body, which runs at *import* time; if these fields lived
    on `Settings` (which inherits a required Postgres password), merely importing the worker —
    as any test reaching `process_backtest` does — would demand a database credential. Keeping
    the Redis half on its own lets that import succeed with nothing configured.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    redis_host: str = "localhost"
    redis_port: int = 6379

    @property
    def redis_url(self) -> str:
        return f"redis://{self.redis_host}:{self.redis_port}/0"


class Settings(PostgresSettings, RedisConfig):
    """Connection settings for the services the API depends on."""

    # Where the collector wrote the Parquet candles (ADR-05). The worker reads a backtest's
    # bars from here; the `datasets` row only proves coverage, the bytes live on disk under
    # this root as `symbol/timeframe/...`. The default matches the collector's own default
    # `--data-dir` (`data/ohlcv`) so a fresh clone's backfill and backtest line up with no
    # configuration. Env-driven (`PARQUET_ROOT`) so dev, CI and prod each point their own.
    parquet_root: Path = Path("data/ohlcv")

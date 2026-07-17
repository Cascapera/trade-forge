"""Settings are read from the environment — and refuse to guess."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from tradeforge_api.config import RedisConfig, Settings
from tradeforge_api.queue import redis_settings


@pytest.fixture(autouse=True)
def _isolate_from_dotenv(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Run from a directory with no `.env`.

    `env_file` is resolved relative to the working directory, so without this a
    developer's local `.env` would leak into the run — and a test whose result
    depends on an untracked file is not a test, it is a coincidence.
    """
    monkeypatch.chdir(tmp_path)


def test_reads_values_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POSTGRES_HOST", "db.internal")
    monkeypatch.setenv("POSTGRES_PORT", "6543")
    monkeypatch.setenv("POSTGRES_DB", "forge")
    monkeypatch.setenv("POSTGRES_USER", "trader")
    monkeypatch.setenv("POSTGRES_PASSWORD", "s3cret")

    settings = Settings()

    assert settings.postgres_host == "db.internal"
    assert settings.postgres_port == 6543
    assert settings.postgres_dsn == "postgresql://trader:s3cret@db.internal:6543/forge"


def test_redis_url_is_composed_from_its_parts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POSTGRES_PASSWORD", "s3cret")
    monkeypatch.setenv("REDIS_HOST", "cache.internal")
    monkeypatch.setenv("REDIS_PORT", "6380")

    assert Settings().redis_url == "redis://cache.internal:6380/0"


def test_redis_settings_need_no_database_password(monkeypatch: pytest.MonkeyPatch) -> None:
    """The worker builds arq's redis settings in its class body, at import time.

    Regression: it used to build the full `Settings` there — which requires POSTGRES_PASSWORD —
    so importing the worker (as any test reaching `process_backtest` does) failed wherever the
    password was unset, and that broke pytest collection in CI. Redis needs only host and port,
    so `redis_settings(RedisConfig())` must succeed with no database credential in the room.
    """
    monkeypatch.delenv("POSTGRES_PASSWORD", raising=False)

    settings = redis_settings(RedisConfig())

    assert settings.port == RedisConfig().redis_port


def test_refuses_to_start_without_a_password(monkeypatch: pytest.MonkeyPatch) -> None:
    """No default password, ever. Failing to boot beats booting insecurely."""
    monkeypatch.delenv("POSTGRES_PASSWORD", raising=False)

    with pytest.raises(ValidationError, match="postgres_password"):
        Settings()


def test_refuses_an_empty_password(monkeypatch: pytest.MonkeyPatch) -> None:
    """An empty string is how "unset" sneaks past a naive check."""
    monkeypatch.setenv("POSTGRES_PASSWORD", "")

    with pytest.raises(ValidationError, match="at least 1 character"):
        Settings()

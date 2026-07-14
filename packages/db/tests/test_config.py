"""The one place a connection string is assembled — so the one place it can break."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from tradeforge_db.config import PostgresSettings


@pytest.fixture(autouse=True)
def _isolate_from_dotenv(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Run from a directory with no `.env`, so a local file cannot leak into the run."""
    monkeypatch.chdir(tmp_path)


@pytest.fixture(autouse=True)
def _password(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POSTGRES_PASSWORD", "s3cret")


def test_libpq_dsn_is_composed_from_its_parts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POSTGRES_HOST", "db.internal")
    monkeypatch.setenv("POSTGRES_PORT", "6543")
    monkeypatch.setenv("POSTGRES_DB", "forge")
    monkeypatch.setenv("POSTGRES_USER", "trader")

    assert PostgresSettings().postgres_dsn == "postgresql://trader:s3cret@db.internal:6543/forge"


def test_sqlalchemy_dsn_pins_psycopg3() -> None:
    """Without `+psycopg`, SQLAlchemy reaches for psycopg2 — which is not installed."""
    assert PostgresSettings().sqlalchemy_dsn.startswith("postgresql+psycopg://")


def test_a_password_with_url_characters_is_escaped(monkeypatch: pytest.MonkeyPatch) -> None:
    """`@` in a password would otherwise cut the URL in half and rename the host.

    The connection would not fail — it would go somewhere else, or fail with an error
    naming a host nobody configured. Percent-encoding keeps the password inside the
    password field.
    """
    monkeypatch.setenv("POSTGRES_PASSWORD", "p@ss/w:rd")

    dsn = PostgresSettings().sqlalchemy_dsn

    assert "p%40ss%2Fw%3Ard" in dsn
    assert "@localhost:5432/tradeforge" in dsn


def test_refuses_to_start_without_a_password(monkeypatch: pytest.MonkeyPatch) -> None:
    """No default password, ever. Failing to boot beats booting insecurely."""
    monkeypatch.delenv("POSTGRES_PASSWORD", raising=False)

    with pytest.raises(ValidationError, match="postgres_password"):
        PostgresSettings()
